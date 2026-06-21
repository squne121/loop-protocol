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

    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    debounce_dir.mkdir(parents=True, exist_ok=True)
    lock_path = debounce_dir / "worker.lock"
    lock_path.write_text("", encoding="utf-8")

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
    assert lock_path.exists()
    assert "flush_skipped_lock_held" in result.stderr
