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


def test_timeout_reason(tmp_path: Path):
    guard_stub = tmp_path / "guard.sh"
    guard_stub.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
    guard_stub.chmod(0o755)

    producer_marker = tmp_path / "producer_called.txt"
    producer_stub = tmp_path / "producer.sh"
    producer_stub.write_text(f"#!/usr/bin/env bash\ntouch {producer_marker}\nexit 0\n", encoding="utf-8")
    producer_stub.chmod(0o755)

    debounce_stub = tmp_path / "debounce.mjs"
    debounce_stub.write_text("process.exit(0)\n", encoding="utf-8")

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

    debounce_stub = tmp_path / "debounce.mjs"
    debounce_stub.write_text("process.exit(0)\n", encoding="utf-8")

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
    producer_stub = tmp_path / "producer.sh"
    producer_stub.write_text(
        f"#!/usr/bin/env bash\necho call >> {producer_log}\ncat >/dev/null\nexit 0\n",
        encoding="utf-8",
    )
    producer_stub.chmod(0o755)

    env = os.environ.copy()
    env["SESSION_MANIFEST_GUARD"] = str(guard_stub)
    env["SESSION_MANIFEST_PRODUCER"] = str(producer_stub)
    env["SESSION_MANIFEST_NODE"] = "bash"
    env["SESSION_MANIFEST_DEBOUNCE_DIR"] = str(debounce_dir)
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_CMD"] = str(producer_stub)
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_ARGS_JSON"] = "[]"

    result = run_coordinator({"hook_event_name": "Stop", "stop_hook_active": False}, env)

    assert result.returncode == 0
    assert producer_log.exists()
    assert "step=debounce_flush" in result.stderr
    assert "step=producer" in result.stderr
