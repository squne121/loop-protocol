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

DEBOUNCE_PATH = REPO_ROOT / ".claude" / "hooks" / "session_manifest_debounce.mjs"


def make_env(tmp_path: Path, producer_script: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["SESSION_MANIFEST_DEBOUNCE_DIR"] = str(tmp_path / "debounce")
    env["SESSION_MANIFEST_DEBOUNCE_WINDOW_MS"] = "80"
    env["SESSION_MANIFEST_DEBOUNCE_FLUSH_WAIT_MS"] = "300"
    env["SESSION_MANIFEST_DEBOUNCE_LOCK_POLL_MS"] = "50"
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_TIMEOUT_MS"] = "250"
    env["SESSION_MANIFEST_DEBOUNCE_WORKER_STALE_MS"] = "1500"
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_CMD"] = str(producer_script)
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_ARGS_JSON"] = "[]"
    return env


def run_front_gate(payload: dict, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
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


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for condition")


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


def test_producer_invocation_is_debounced_for_bash_burst(tmp_path: Path):
    log_path = tmp_path / "producer.log"
    producer = tmp_path / "producer.sh"
    producer.write_text(
        f"#!/usr/bin/env bash\necho call >> {log_path}\ncat >/dev/null\nexit 0\n",
        encoding="utf-8",
    )
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "cwd": str(REPO_ROOT),
        "tool_input": {"command": "pnpm test .claude/hooks/tests/test_session_manifest_debounce.py"},
    }
    for _ in range(10):
        result = run_front_gate(payload, env)
        assert result.returncode == 0
        assert result.stdout == ""

    wait_for(lambda: log_path.exists())
    assert log_path.read_text(encoding="utf-8").count("call") == 1


def test_readonly_bash_skips_producer(tmp_path: Path):
    log_path = tmp_path / "producer.log"
    producer = tmp_path / "producer.sh"
    producer.write_text(
        f"#!/usr/bin/env bash\necho call >> {log_path}\ncat >/dev/null\nexit 0\n",
        encoding="utf-8",
    )
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "cwd": str(REPO_ROOT),
        "tool_input": {"command": "rg -n debounce .claude/hooks/tests"},
    }
    for _ in range(10):
        result = run_front_gate(payload, env)
        assert result.returncode == 0
        assert result.stdout == ""

    time.sleep(0.3)
    assert not log_path.exists()


def test_sanitized_delta(tmp_path: Path):
    log_path = tmp_path / "payload.json"
    producer = tmp_path / "producer.py"
    producer.write_text(
        f"""#!/usr/bin/env python3
import json
import pathlib
import sys
payload = json.load(sys.stdin)
pathlib.Path({str(log_path)!r}).write_text(json.dumps(payload), encoding="utf-8")
""",
        encoding="utf-8",
    )
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "cwd": str(REPO_ROOT),
        "tool_input": {"file_path": str(REPO_ROOT / "docs" / "dev" / "hook-boundaries.md")},
    }
    result = run_front_gate(payload, env)
    assert result.returncode == 0

    wait_for(lambda: log_path.exists())
    flushed = json.loads(log_path.read_text(encoding="utf-8"))
    delta = flushed["session_manifest_delta"][0]
    assert delta["mutation_type"] == "write"
    assert delta["relative_paths"] == ["docs/dev/hook-boundaries.md"]
    assert str(REPO_ROOT) not in json.dumps(flushed)


def test_event_spool_is_sanitized_without_raw_paths(tmp_path: Path):
    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)

    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    lock_path = debounce_dir / "worker.lock"
    write_lock(lock_path, owner_pid=os.getpid(), heartbeat_at_ms=int(time.time() * 1000))

    transcript_path = REPO_ROOT / "artifacts" / "private-transcript.json"
    result = run_front_gate(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "cwd": str(REPO_ROOT),
            "transcript_path": str(transcript_path),
            "tool_input": {"file_path": str(REPO_ROOT / "docs" / "dev" / "hook-boundaries.md")},
        },
        env,
    )
    assert result.returncode == 0

    event_files = sorted((debounce_dir / "events").glob("*.json"))
    assert event_files, "expected queued debounce event"
    event_payload = json.loads(event_files[0].read_text(encoding="utf-8"))
    serialized = json.dumps(event_payload)
    assert "cwd" not in event_payload
    assert "transcript_path" not in serialized
    assert str(REPO_ROOT) not in serialized
    assert event_payload["session_manifest_delta"][0]["relative_paths"] == ["docs/dev/hook-boundaries.md"]


def test_stdout_silent(tmp_path: Path):
    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)

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


def test_stderr_redaction(tmp_path: Path):
    producer = tmp_path / "producer.sh"
    producer.write_text(
        """#!/usr/bin/env bash
for i in $(seq 1 100); do
  echo "/home/private/$i C:\\\\Users\\\\Private\\\\$i /mnt/c/Users/Private/$i" >&2
done
exit 1
""",
        encoding="utf-8",
    )
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)

    queue_result = run_front_gate(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "cwd": str(REPO_ROOT),
            "tool_input": {"file_path": str(REPO_ROOT / ".claude" / "settings.json")},
        },
        env,
    )
    assert queue_result.returncode == 0

    flush_result = subprocess.run(
        ["node", str(DEBOUNCE_PATH), "--flush"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )
    lines = [line for line in flush_result.stderr.splitlines() if line.strip()]
    assert flush_result.returncode == 0
    assert len(lines) <= 10
    assert "/home/private" not in flush_result.stderr
    assert "C:\\Users\\Private" not in flush_result.stderr
    assert "/mnt/c/Users/Private" not in flush_result.stderr


def test_flush_does_not_release_unowned_worker_lock(tmp_path: Path):
    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)
    env["SESSION_MANIFEST_DEBOUNCE_WORKER_STALE_MS"] = "10000"

    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    debounce_dir.mkdir(parents=True, exist_ok=True)
    events_dir = debounce_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "pending.json").write_text(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "lock-held",
                "session_manifest_delta": [{"mutation_type": "write", "relative_paths": ["docs/dev/hook-boundaries.md"]}],
            }
        ),
        encoding="utf-8",
    )
    lock_path = debounce_dir / "worker.lock"
    write_lock(lock_path, owner_pid=os.getpid(), heartbeat_at_ms=int(time.time() * 1000))

    result = subprocess.run(
        ["node", str(DEBOUNCE_PATH), "--flush"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )

    assert result.returncode == 124
    assert lock_path.exists()
    assert "flush_pending_timeout_lock_held" in result.stderr


def test_flush_recovers_stale_lock_and_forces_pending_events(tmp_path: Path):
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
    (events_dir / f"{int(time.time() * 1000)}-pending.json").write_text(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "stale-lock",
                "session_manifest_delta": [{"mutation_type": "write", "relative_paths": ["docs/dev/hook-boundaries.md"]}],
            }
        ),
        encoding="utf-8",
    )

    lock_path = debounce_dir / "worker.lock"
    write_lock(lock_path, owner_pid=999999, heartbeat_at_ms=1, started_at_ms=1)

    result = subprocess.run(
        ["node", str(DEBOUNCE_PATH), "--flush"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0
    wait_for(lambda: log_path.exists())
    assert log_path.read_text(encoding="utf-8").count("call") == 1
    assert not lock_path.exists()


def test_mutating_bash_patterns_are_queued(tmp_path: Path):
    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)
    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    lock_path = debounce_dir / "worker.lock"
    commands = [
        "sed -i 's/foo/bar/' docs/dev/hook-boundaries.md",
        "find . -name '*.tmp' -delete",
        "git diff --output=patch.diff",
    ]

    for command in commands:
        write_lock(lock_path, owner_pid=os.getpid(), heartbeat_at_ms=int(time.time() * 1000))
        result = run_front_gate(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "cwd": str(REPO_ROOT),
                "tool_input": {"command": command},
            },
            env,
        )
        assert result.returncode == 0
        event_files = sorted((debounce_dir / "events").glob("*.json"))
        assert event_files, f"expected queued event for: {command}"
        event_payload = json.loads(event_files[-1].read_text(encoding="utf-8"))
        assert event_payload["session_manifest_delta"][0]["mutation_type"] != "readonly_bash"
        rm_files = list((debounce_dir / "events").glob("*.json"))
        for event_file in rm_files:
            event_file.unlink()

    assert lock_path.exists()


def test_worker_times_out_producer_and_releases_lock(tmp_path: Path):
    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)
    env["SESSION_MANIFEST_DEBOUNCE_WINDOW_MS"] = "0"

    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    events_dir = debounce_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "1-pending.json").write_text(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "worker-timeout",
                "session_manifest_delta": [{"mutation_type": "write", "relative_paths": ["docs/dev/hook-boundaries.md"]}],
            }
        ),
        encoding="utf-8",
    )
    lock_path = debounce_dir / "worker.lock"
    write_lock(lock_path, owner_pid=os.getpid(), heartbeat_at_ms=int(time.time() * 1000))

    result = subprocess.run(
        ["node", str(DEBOUNCE_PATH), "--worker"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0
    assert "producer_timeout" in result.stderr
    assert not lock_path.exists()
