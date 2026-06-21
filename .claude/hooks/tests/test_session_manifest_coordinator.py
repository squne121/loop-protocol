#!/usr/bin/env python3

import json
import os
import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
)

COORDINATOR_PATH = REPO_ROOT / ".claude" / "hooks" / "session_manifest_coordinator.sh"


def run_coordinator(payload: dict, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(COORDINATOR_PATH)],
        cwd=REPO_ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )


def write_lock(lock_path: Path, *, owner_pid: int, heartbeat_at_ms: int, started_at_ms: int | None = None) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "owner_pid": owner_pid,
                "role": "worker",
                "started_at_ms": started_at_ms if started_at_ms is not None else heartbeat_at_ms,
                "heartbeat_at_ms": heartbeat_at_ms,
            }
        ),
        encoding="utf-8",
    )


def test_timeout_reason(tmp_path: Path):
    guard_stub = tmp_path / "guard.sh"
    guard_stub.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
    guard_stub.chmod(0o755)

    producer_marker = tmp_path / "producer_called.txt"
    producer_stub = tmp_path / "producer.sh"
    producer_stub.write_text(f"#!/usr/bin/env bash\ntouch {producer_marker}\nexit 0\n", encoding="utf-8")
    producer_stub.chmod(0o755)

    debounce_stub = tmp_path / "debounce.sh"
    debounce_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    debounce_stub.chmod(0o755)

    env = os.environ.copy()
    env["SESSION_MANIFEST_GUARD"] = str(guard_stub)
    env["SESSION_MANIFEST_PRODUCER"] = str(producer_stub)
    env["SESSION_MANIFEST_NODE"] = "bash"
    env["SESSION_MANIFEST_DEBOUNCE_SCRIPT"] = str(debounce_stub)
    env["SESSION_MANIFEST_COORDINATOR_STEP_TIMEOUT_SECONDS"] = "1"

    started = time.monotonic()
    result = run_coordinator({"hook_event_name": "Stop", "stop_hook_active": False}, env)
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert elapsed < 10
    assert '"timeout_reason":"guard_timeout"' in result.stderr
    assert not producer_marker.exists()
    assert "step=producer" not in result.stderr


def test_coordinator_limits_stderr_to_ten_lines_and_redacts_paths(tmp_path: Path):
    guard_stub = tmp_path / "guard.sh"
    guard_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    guard_stub.chmod(0o755)

    producer_stub = tmp_path / "producer.sh"
    producer_stub.write_text(
        """#!/usr/bin/env bash
for i in $(seq 1 100); do
  echo "/home/private/$i C:\\\\Users\\\\Private\\\\$i /mnt/c/Users/Private/$i" >&2
done
exit 1
""",
        encoding="utf-8",
    )
    producer_stub.chmod(0o755)

    debounce_stub = tmp_path / "debounce.sh"
    debounce_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    debounce_stub.chmod(0o755)

    env = os.environ.copy()
    env["SESSION_MANIFEST_GUARD"] = str(guard_stub)
    env["SESSION_MANIFEST_PRODUCER"] = str(producer_stub)
    env["SESSION_MANIFEST_NODE"] = "bash"
    env["SESSION_MANIFEST_DEBOUNCE_SCRIPT"] = str(debounce_stub)
    env["SESSION_MANIFEST_COORDINATOR_STEP_TIMEOUT_SECONDS"] = "2"

    result = run_coordinator({"hook_event_name": "Stop", "stop_hook_active": False}, env)

    stderr_lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert result.returncode == 0
    assert len(stderr_lines) <= 10
    assert "/home/private" not in result.stderr
    assert "C:\\Users\\Private" not in result.stderr
    assert "/mnt/c/Users/Private" not in result.stderr


def test_coordinator_flushes_pending_debounce_before_running_producer(tmp_path: Path):
    debounce_dir = tmp_path / "debounce"
    events_dir = debounce_dir / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "pending.json").write_text(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "cwd": str(REPO_ROOT),
                "delta": [{"mutation_type": "write", "relative_paths": ["docs/dev/hook-boundaries.md"]}],
            }
        ),
        encoding="utf-8",
    )

    guard_stub = tmp_path / "guard.sh"
    guard_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    guard_stub.chmod(0o755)

    producer_log = tmp_path / "producer.log"
    producer_stub = tmp_path / "producer.mjs"
    producer_stub.write_text(
        f"""
import {{ appendFileSync }} from 'node:fs'
process.stdin.resume()
process.stdin.on('end', () => {{
  appendFileSync({str(producer_log)!r}, 'call\\n')
}})
""".strip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["SESSION_MANIFEST_GUARD"] = str(guard_stub)
    env["SESSION_MANIFEST_PRODUCER"] = str(producer_stub)
    env["SESSION_MANIFEST_NODE"] = "node"
    env["SESSION_MANIFEST_DEBOUNCE_DIR"] = str(debounce_dir)
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_CMD"] = str(producer_stub)
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_ARGS_JSON"] = "[]"

    result = run_coordinator({"hook_event_name": "Stop", "stop_hook_active": False}, env)

    assert result.returncode == 0
    assert producer_log.exists()
    assert "step=debounce_flush" in result.stderr
    assert "step=producer" in result.stderr


def test_coordinator_reports_debounce_flush_timeout_when_worker_lock_stays_live(tmp_path: Path):
    debounce_dir = tmp_path / "debounce"
    events_dir = debounce_dir / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "pending.json").write_text(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_manifest_delta": [{"mutation_type": "write", "relative_paths": ["docs/dev/hook-boundaries.md"]}],
            }
        ),
        encoding="utf-8",
    )
    write_lock(debounce_dir / "worker.lock", owner_pid=os.getpid(), heartbeat_at_ms=int(time.time() * 1000))

    guard_stub = tmp_path / "guard.sh"
    guard_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    guard_stub.chmod(0o755)

    producer_log = tmp_path / "producer.log"
    producer_stub = tmp_path / "producer.mjs"
    producer_stub.write_text(
        f"""
import {{ appendFileSync }} from 'node:fs'
process.stdin.resume()
process.stdin.on('end', () => {{
  appendFileSync({str(producer_log)!r}, 'call\\n')
}})
""".strip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["SESSION_MANIFEST_GUARD"] = str(guard_stub)
    env["SESSION_MANIFEST_PRODUCER"] = str(producer_stub)
    env["SESSION_MANIFEST_NODE"] = "node"
    env["SESSION_MANIFEST_DEBOUNCE_DIR"] = str(debounce_dir)
    env["SESSION_MANIFEST_DEBOUNCE_SCRIPT"] = str(REPO_ROOT / ".claude" / "hooks" / "session_manifest_debounce.mjs")
    env["SESSION_MANIFEST_DEBOUNCE_FLUSH_WAIT_MS"] = "200"
    env["SESSION_MANIFEST_DEBOUNCE_LOCK_POLL_MS"] = "50"
    env["SESSION_MANIFEST_COORDINATOR_STEP_TIMEOUT_SECONDS"] = "2"

    result = run_coordinator({"hook_event_name": "Stop", "stop_hook_active": False}, env)

    assert result.returncode == 0
    assert producer_log.exists()
    assert "step=debounce_flush status=timeout reason_code=debounce_flush_timeout" in result.stderr
    assert '"timeout_reason":"debounce_flush_timeout"' in result.stderr


def test_scope_rollup_capture_is_redacted_and_bounded(tmp_path: Path):
    guard_stub = tmp_path / "guard.sh"
    guard_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    guard_stub.chmod(0o755)

    producer_stub = tmp_path / "producer.sh"
    producer_stub.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    producer_stub.chmod(0o755)

    capture_stub = tmp_path / "capture.py"
    capture_stub.write_text(
        """
import sys
import time
for i in range(100):
    print(f"/home/private/{i} C:\\\\Users\\\\Private\\\\{i} /mnt/c/Users/Private/{i}", file=sys.stderr)
time.sleep(5)
""".strip(),
        encoding="utf-8",
    )

    debounce_stub = tmp_path / "debounce.sh"
    debounce_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    debounce_stub.chmod(0o755)

    env = os.environ.copy()
    env["SESSION_MANIFEST_GUARD"] = str(guard_stub)
    env["SESSION_MANIFEST_PRODUCER"] = str(producer_stub)
    env["SESSION_MANIFEST_NODE"] = "bash"
    env["SESSION_MANIFEST_DEBOUNCE_SCRIPT"] = str(debounce_stub)
    env["SESSION_MANIFEST_COORDINATOR_STEP_TIMEOUT_SECONDS"] = "1"
    env["SCOPE_ROLLUP_CAPTURE_SCRIPT"] = str(capture_stub)
    env["SCOPE_ROLLUP_CAPTURE_PYTHON"] = "python3"
    env["SCOPE_ROLLUP_CAPTURE_DIR"] = str(tmp_path)

    result = run_coordinator({"hook_event_name": "Stop", "stop_hook_active": False}, env)
    stderr_lines = [line for line in result.stderr.splitlines() if line.strip()]

    assert result.returncode == 0
    assert len(stderr_lines) <= 10
    assert "step=scope_rollup_capture status=timeout reason_code=scope_rollup_capture_timeout" in result.stderr
    assert "/home/private" not in result.stderr
    assert "C:\\Users\\Private" not in result.stderr
    assert "/mnt/c/Users/Private" not in result.stderr
