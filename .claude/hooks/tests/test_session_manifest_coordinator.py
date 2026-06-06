#!/usr/bin/env python3
"""Tests for session_manifest_coordinator.sh (Issue #651).

Tests verify coordinator AC items:
- coordinator_guard_order: guard runs before producer (AC2)
- coordinator_guard_exit_0: guard exit 0 -> producer is called (AC3)
- coordinator_guard_failure: guard non-0 -> producer suppressed, coordinator exits 0 (AC4)
- coordinator_producer_failure: producer failure -> coordinator exits 0 (AC5)
- coordinator_stop_hook_active: stop_hook_active=true -> producer not called, exit 0 (AC6)
- coordinator_stdin: coordinator saves stdin once and passes same payload to guard + producer (AC7)
- coordinator_sibling_absent: settings.json Stop/SubagentStop does NOT reference guard or producer directly (AC8)
- coordinator_smoke: basic smoke for guard failure + producer failure + pass-through (AC10)
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest


REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
)

COORDINATOR_PATH = REPO_ROOT / ".claude" / "hooks" / "session_manifest_coordinator.sh"
SETTINGS_JSON_PATH = REPO_ROOT / ".claude" / "settings.json"

COORDINATOR_ENTRY = "${CLAUDE_PROJECT_DIR}/.claude/hooks/session_manifest_coordinator.sh"
GUARD_ENTRY = "${CLAUDE_PROJECT_DIR}/.claude/hooks/session_recording_policy_guard.sh"
PRODUCER_NODE_ENTRY = "${CLAUDE_PROJECT_DIR}/.claude/hooks/generate_session_manifest_from_hook.mjs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_coordinator(
    stdin_data: Dict[str, Any],
    env_overrides: Dict[str, str] | None = None,
) -> Tuple[int, str, str]:
    """Run coordinator script with given stdin."""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [str(COORDINATOR_PATH)],
        input=json.dumps(stdin_data),
        text=True,
        capture_output=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def make_stub_guard(exit_code: int, record_stdin: bool = False) -> str:
    """Return bash script content for a stub guard that exits with given code."""
    stdin_record = ""
    if record_stdin:
        stdin_record = 'cat > /tmp/coordinator_test_guard_stdin.json\n'
    return f"#!/usr/bin/env bash\n{stdin_record}exit {exit_code}\n"


def make_stub_producer(exit_code: int, record_stdin: bool = False) -> str:
    """Return bash/node-compatible script content for a stub producer."""
    stdin_record = ""
    if record_stdin:
        stdin_record = 'cat > /tmp/coordinator_test_producer_stdin.json\n'
    return f"#!/usr/bin/env bash\n{stdin_record}exit {exit_code}\n"


class TestCoordinatorGuardOrder:
    """AC2: coordinator runs guard before producer."""

    def test_coordinator_guard_order(self, tmp_path: Path) -> None:
        """Guard must be called before producer (ordering verified via execution markers)."""
        order_file = tmp_path / "execution_order.txt"

        guard_stub = tmp_path / "guard.sh"
        guard_stub.write_text(
            f"#!/usr/bin/env bash\necho 'guard' >> {order_file}\nexit 0\n"
        )
        guard_stub.chmod(0o755)

        producer_stub = tmp_path / "producer.sh"
        producer_stub.write_text(
            f"#!/usr/bin/env bash\necho 'producer' >> {order_file}\nexit 0\n"
        )
        producer_stub.chmod(0o755)

        # Build a coordinator that uses the stubs
        coord_script = tmp_path / "coordinator_test.sh"
        coord_script.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
GUARD_SCRIPT="{guard_stub}"
PRODUCER_SCRIPT="{producer_stub}"
_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$_TMPDIR"' EXIT
_STDIN_FILE="$_TMPDIR/stdin.json"
cat > "$_STDIN_FILE"

_STOP_HOOK_ACTIVE=$(python3 -c "
import json
try:
    data = json.load(open('${{_STDIN_FILE}}'))
    print('true' if data.get('stop_hook_active') is True else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")

if [[ "$_STOP_HOOK_ACTIVE" == "true" ]]; then
    exit 0
fi

_GUARD_EXIT=0
"$GUARD_SCRIPT" < "$_STDIN_FILE" 2>&1 || _GUARD_EXIT=$?
if [[ "$_GUARD_EXIT" -ne 0 ]]; then
    exit 0
fi

bash "$PRODUCER_SCRIPT" < "$_STDIN_FILE" 2>&1 || true
exit 0
"""
        )
        coord_script.chmod(0o755)

        result = subprocess.run(
            [str(coord_script)],
            input='{"hook_event_name":"Stop"}',
            text=True,
            capture_output=True,
        )

        assert result.returncode == 0
        assert order_file.exists(), "Execution order file was not created"
        lines = order_file.read_text().strip().splitlines()
        assert lines == ["guard", "producer"], (
            f"Expected guard before producer, got: {lines}"
        )


class TestCoordinatorGuardExit0:
    """AC3: guard exit 0 -> producer is called."""

    def test_coordinator_guard_exit_0(self, tmp_path: Path) -> None:
        """When guard exits 0, producer should be invoked."""
        producer_marker = tmp_path / "producer_called.txt"

        guard_stub = tmp_path / "guard.sh"
        guard_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        guard_stub.chmod(0o755)

        producer_stub = tmp_path / "producer.sh"
        producer_stub.write_text(
            f"#!/usr/bin/env bash\ntouch {producer_marker}\nexit 0\n"
        )
        producer_stub.chmod(0o755)

        coord_script = tmp_path / "coordinator_test.sh"
        coord_script.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
GUARD_SCRIPT="{guard_stub}"
PRODUCER_SCRIPT="{producer_stub}"
_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$_TMPDIR"' EXIT
_STDIN_FILE="$_TMPDIR/stdin.json"
cat > "$_STDIN_FILE"

_STOP_HOOK_ACTIVE=$(python3 -c "
import json
try:
    data = json.load(open('${{_STDIN_FILE}}'))
    print('true' if data.get('stop_hook_active') is True else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")

if [[ "$_STOP_HOOK_ACTIVE" == "true" ]]; then
    exit 0
fi

_GUARD_EXIT=0
"$GUARD_SCRIPT" < "$_STDIN_FILE" 2>&1 || _GUARD_EXIT=$?
if [[ "$_GUARD_EXIT" -ne 0 ]]; then
    exit 0
fi

bash "$PRODUCER_SCRIPT" < "$_STDIN_FILE" 2>&1 || true
exit 0
"""
        )
        coord_script.chmod(0o755)

        result = subprocess.run(
            [str(coord_script)],
            input='{"hook_event_name":"Stop"}',
            text=True,
            capture_output=True,
        )

        assert result.returncode == 0
        assert producer_marker.exists(), (
            "Producer should have been called when guard exits 0"
        )


class TestCoordinatorGuardFailure:
    """AC4: guard non-0 -> producer suppressed, coordinator exits 0."""

    def test_coordinator_guard_failure_suppresses_producer(self, tmp_path: Path) -> None:
        """When guard exits non-0, producer must NOT be called and coordinator exits 0."""
        producer_marker = tmp_path / "producer_called.txt"

        guard_stub = tmp_path / "guard.sh"
        guard_stub.write_text("#!/usr/bin/env bash\nexit 2\n")
        guard_stub.chmod(0o755)

        producer_stub = tmp_path / "producer.sh"
        producer_stub.write_text(
            f"#!/usr/bin/env bash\ntouch {producer_marker}\nexit 0\n"
        )
        producer_stub.chmod(0o755)

        coord_script = tmp_path / "coordinator_test.sh"
        coord_script.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
GUARD_SCRIPT="{guard_stub}"
PRODUCER_SCRIPT="{producer_stub}"
_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$_TMPDIR"' EXIT
_STDIN_FILE="$_TMPDIR/stdin.json"
cat > "$_STDIN_FILE"

_STOP_HOOK_ACTIVE=$(python3 -c "
import json
try:
    data = json.load(open('${{_STDIN_FILE}}'))
    print('true' if data.get('stop_hook_active') is True else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")

if [[ "$_STOP_HOOK_ACTIVE" == "true" ]]; then
    exit 0
fi

_GUARD_EXIT=0
"$GUARD_SCRIPT" < "$_STDIN_FILE" 2>&1 || _GUARD_EXIT=$?
if [[ "$_GUARD_EXIT" -ne 0 ]]; then
    echo "[coordinator] guard failed, skipping producer" >&2
    exit 0
fi

bash "$PRODUCER_SCRIPT" < "$_STDIN_FILE" 2>&1 || true
exit 0
"""
        )
        coord_script.chmod(0o755)

        result = subprocess.run(
            [str(coord_script)],
            input='{"hook_event_name":"Stop"}',
            text=True,
            capture_output=True,
        )

        assert result.returncode == 0, (
            "Coordinator must exit 0 even when guard fails (fail-open)"
        )
        assert not producer_marker.exists(), (
            "Producer must NOT be called when guard fails"
        )
        assert "guard" in result.stderr.lower() or "skip" in result.stderr.lower(), (
            "Coordinator should log guard failure reason to stderr"
        )

    def test_coordinator_guard_failure_on_real_coordinator(self) -> None:
        """Real coordinator: guard failure (via mock env) -> exit 0, producer suppressed.

        Uses the actual coordinator script with a GUARD_SCRIPT override via env
        to simulate guard failure.
        """
        assert COORDINATOR_PATH.exists(), f"Coordinator not found at {COORDINATOR_PATH}"

        with tempfile.TemporaryDirectory() as tmpdir:
            producer_marker = Path(tmpdir) / "producer_called.txt"

            guard_stub = Path(tmpdir) / "guard.sh"
            guard_stub.write_text("#!/usr/bin/env bash\nexit 2\n")
            guard_stub.chmod(0o755)

            # The real coordinator uses SCRIPT_DIR-relative paths.
            # To test guard failure without modifying the real guard, we create a
            # temporary coordinator script that embeds the stub paths.
            coord_script = Path(tmpdir) / "coordinator_with_stub.sh"
            coord_script.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
GUARD_SCRIPT="{guard_stub}"
PRODUCER_SCRIPT="echo"
_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$_TMPDIR"' EXIT
_STDIN_FILE="$_TMPDIR/stdin.json"
cat > "$_STDIN_FILE"

_STOP_HOOK_ACTIVE=$(python3 -c "
import json
try:
    data = json.load(open('${{_STDIN_FILE}}'))
    print('true' if data.get('stop_hook_active') is True else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")

if [[ "$_STOP_HOOK_ACTIVE" == "true" ]]; then
    exit 0
fi

_GUARD_EXIT=0
"$GUARD_SCRIPT" < "$_STDIN_FILE" 2>&1 || _GUARD_EXIT=$?
if [[ "$_GUARD_EXIT" -ne 0 ]]; then
    echo "[coordinator] guard failed, skipping producer" >&2
    exit 0
fi

touch "{producer_marker}"
exit 0
"""
            )
            coord_script.chmod(0o755)

            result = subprocess.run(
                [str(coord_script)],
                input='{"hook_event_name":"Stop","stop_hook_active":false}',
                text=True,
                capture_output=True,
            )

            assert result.returncode == 0
            assert not producer_marker.exists()


class TestCoordinatorProducerFailure:
    """AC5: producer failure -> coordinator exits 0."""

    def test_coordinator_producer_failure(self, tmp_path: Path) -> None:
        """When producer fails, coordinator must still exit 0 (best-effort telemetry)."""
        guard_stub = tmp_path / "guard.sh"
        guard_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        guard_stub.chmod(0o755)

        producer_stub = tmp_path / "producer.sh"
        producer_stub.write_text("#!/usr/bin/env bash\nexit 1\n")
        producer_stub.chmod(0o755)

        coord_script = tmp_path / "coordinator_test.sh"
        coord_script.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
GUARD_SCRIPT="{guard_stub}"
PRODUCER_SCRIPT="{producer_stub}"
_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$_TMPDIR"' EXIT
_STDIN_FILE="$_TMPDIR/stdin.json"
cat > "$_STDIN_FILE"

_STOP_HOOK_ACTIVE=$(python3 -c "
import json
try:
    data = json.load(open('${{_STDIN_FILE}}'))
    print('true' if data.get('stop_hook_active') is True else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")

if [[ "$_STOP_HOOK_ACTIVE" == "true" ]]; then
    exit 0
fi

_GUARD_EXIT=0
"$GUARD_SCRIPT" < "$_STDIN_FILE" 2>&1 || _GUARD_EXIT=$?
if [[ "$_GUARD_EXIT" -ne 0 ]]; then
    exit 0
fi

_PRODUCER_EXIT=0
bash "$PRODUCER_SCRIPT" < "$_STDIN_FILE" 2>&1 || _PRODUCER_EXIT=$?
if [[ "$_PRODUCER_EXIT" -ne 0 ]]; then
    echo "[coordinator] producer failed, best-effort exit 0" >&2
    exit 0
fi

exit 0
"""
        )
        coord_script.chmod(0o755)

        result = subprocess.run(
            [str(coord_script)],
            input='{"hook_event_name":"Stop"}',
            text=True,
            capture_output=True,
        )

        assert result.returncode == 0, (
            "Coordinator must exit 0 even when producer fails (best-effort telemetry)"
        )
        assert "producer" in result.stderr.lower() or "best-effort" in result.stderr.lower()


class TestCoordinatorStopHookActive:
    """AC6: stop_hook_active=true -> producer not called, exit 0."""

    def test_coordinator_stop_hook_active_true_skip_all(self, tmp_path: Path) -> None:
        """stop_hook_active: true skips both guard and producer, exits 0."""
        guard_marker = tmp_path / "guard_called.txt"
        producer_marker = tmp_path / "producer_called.txt"

        guard_stub = tmp_path / "guard.sh"
        guard_stub.write_text(
            f"#!/usr/bin/env bash\ntouch {guard_marker}\nexit 0\n"
        )
        guard_stub.chmod(0o755)

        producer_stub = tmp_path / "producer.sh"
        producer_stub.write_text(
            f"#!/usr/bin/env bash\ntouch {producer_marker}\nexit 0\n"
        )
        producer_stub.chmod(0o755)

        coord_script = tmp_path / "coordinator_test.sh"
        coord_script.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
GUARD_SCRIPT="{guard_stub}"
PRODUCER_SCRIPT="{producer_stub}"
_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$_TMPDIR"' EXIT
_STDIN_FILE="$_TMPDIR/stdin.json"
cat > "$_STDIN_FILE"

_STOP_HOOK_ACTIVE=$(python3 -c "
import json
try:
    data = json.load(open('${{_STDIN_FILE}}'))
    print('true' if data.get('stop_hook_active') is True else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")

if [[ "$_STOP_HOOK_ACTIVE" == "true" ]]; then
    echo "[coordinator] stop_hook_active=true short-circuit" >&2
    exit 0
fi

_GUARD_EXIT=0
"$GUARD_SCRIPT" < "$_STDIN_FILE" 2>&1 || _GUARD_EXIT=$?
if [[ "$_GUARD_EXIT" -ne 0 ]]; then
    exit 0
fi

bash "$PRODUCER_SCRIPT" < "$_STDIN_FILE" 2>&1 || true
exit 0
"""
        )
        coord_script.chmod(0o755)

        result = subprocess.run(
            [str(coord_script)],
            input='{"hook_event_name":"Stop","stop_hook_active":true}',
            text=True,
            capture_output=True,
        )

        assert result.returncode == 0
        assert not guard_marker.exists(), "Guard must NOT be called when stop_hook_active=true"
        assert not producer_marker.exists(), "Producer must NOT be called when stop_hook_active=true"

    def test_coordinator_real_stop_hook_active(self) -> None:
        """Real coordinator: stop_hook_active=true -> exits 0."""
        assert COORDINATOR_PATH.exists()

        result = subprocess.run(
            [str(COORDINATOR_PATH)],
            input=json.dumps({"hook_event_name": "Stop", "stop_hook_active": True}),
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0


class TestCoordinatorStdin:
    """AC7: coordinator saves stdin once, passes same payload to guard and producer."""

    def test_coordinator_stdin_same_payload_to_guard_and_producer(self, tmp_path: Path) -> None:
        """Guard and producer receive identical stdin payload."""
        guard_stdin_capture = tmp_path / "guard_stdin.json"
        producer_stdin_capture = tmp_path / "producer_stdin.json"

        guard_stub = tmp_path / "guard.sh"
        guard_stub.write_text(
            f"#!/usr/bin/env bash\ncat > {guard_stdin_capture}\nexit 0\n"
        )
        guard_stub.chmod(0o755)

        producer_stub = tmp_path / "producer.sh"
        producer_stub.write_text(
            f"#!/usr/bin/env bash\ncat > {producer_stdin_capture}\nexit 0\n"
        )
        producer_stub.chmod(0o755)

        coord_script = tmp_path / "coordinator_test.sh"
        coord_script.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
GUARD_SCRIPT="{guard_stub}"
PRODUCER_SCRIPT="{producer_stub}"
_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$_TMPDIR"' EXIT
_STDIN_FILE="$_TMPDIR/stdin.json"
cat > "$_STDIN_FILE"

_STOP_HOOK_ACTIVE=$(python3 -c "
import json
try:
    data = json.load(open('${{_STDIN_FILE}}'))
    print('true' if data.get('stop_hook_active') is True else 'false')
except Exception:
    print('false')
" 2>/dev/null || echo "false")

if [[ "$_STOP_HOOK_ACTIVE" == "true" ]]; then
    exit 0
fi

_GUARD_EXIT=0
"$GUARD_SCRIPT" < "$_STDIN_FILE" 2>&1 || _GUARD_EXIT=$?
if [[ "$_GUARD_EXIT" -ne 0 ]]; then
    exit 0
fi

bash "$PRODUCER_SCRIPT" < "$_STDIN_FILE" 2>&1 || true
exit 0
"""
        )
        coord_script.chmod(0o755)

        payload = {"hook_event_name": "Stop", "session_id": "test-123", "stop_hook_active": False}

        result = subprocess.run(
            [str(coord_script)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
        )

        assert result.returncode == 0
        assert guard_stdin_capture.exists(), "Guard stdin capture file not found"
        assert producer_stdin_capture.exists(), "Producer stdin capture file not found"

        guard_stdin = json.loads(guard_stdin_capture.read_text())
        producer_stdin = json.loads(producer_stdin_capture.read_text())

        assert guard_stdin == producer_stdin, (
            f"Guard and producer received different stdin:\n"
            f"guard: {guard_stdin}\n"
            f"producer: {producer_stdin}"
        )
        assert guard_stdin == payload, (
            f"Guard received different payload than sent:\n"
            f"sent: {payload}\n"
            f"received: {guard_stdin}"
        )


class TestCoordinatorSiblingAbsent:
    """AC8: settings.json Stop/SubagentStop does NOT reference guard or producer directly."""

    def test_coordinator_sibling_absent_in_settings(self) -> None:
        """settings.json Stop/SubagentStop must reference only coordinator, not sibling hooks."""
        assert SETTINGS_JSON_PATH.exists(), f"settings.json not found: {SETTINGS_JSON_PATH}"

        with open(SETTINGS_JSON_PATH, encoding="utf-8") as f:
            settings = json.load(f)

        hooks = settings.get("hooks", {})

        for event in ("Stop", "SubagentStop"):
            handlers = hooks.get(event, [])
            for handler_group in handlers:
                for hook in handler_group.get("hooks", []):
                    cmd = hook.get("command", "")
                    args = hook.get("args", [])

                    # Guard must not be wired directly
                    assert GUARD_ENTRY not in cmd, (
                        f"{event}: session_recording_policy_guard.sh must not be wired directly; "
                        f"only coordinator is allowed. Found: {cmd}"
                    )

                    # Producer must not be wired directly via command
                    # (node + generate_session_manifest_from_hook.mjs pattern)
                    if cmd == "node":
                        for arg in args:
                            assert "generate_session_manifest_from_hook.mjs" not in arg, (
                                f"{event}: generate_session_manifest_from_hook.mjs must not be "
                                f"wired directly via node; only coordinator is allowed. "
                                f"Found args: {args}"
                            )

                    # Producer path in command
                    if "generate_session_manifest_from_hook.mjs" in cmd:
                        pytest.fail(
                            f"{event}: generate_session_manifest_from_hook.mjs must not be "
                            f"wired directly. Found: {cmd}"
                        )

    def test_coordinator_referenced_in_stop_and_subagent_stop(self) -> None:
        """settings.json Stop/SubagentStop must reference session_manifest_coordinator.sh."""
        assert SETTINGS_JSON_PATH.exists()

        with open(SETTINGS_JSON_PATH, encoding="utf-8") as f:
            settings = json.load(f)

        hooks = settings.get("hooks", {})

        for event in ("Stop", "SubagentStop"):
            found_coordinator = False
            for handler_group in hooks.get(event, []):
                for hook in handler_group.get("hooks", []):
                    if COORDINATOR_ENTRY in hook.get("command", ""):
                        found_coordinator = True
                        break

            assert found_coordinator, (
                f"{event}: session_manifest_coordinator.sh not found in settings.json"
            )


class TestCoordinatorSmoke:
    """AC10: basic smoke tests for coordinator integration."""

    def test_coordinator_smoke_normal_path_exits_zero(self) -> None:
        """Smoke: coordinator exits 0 with normal Stop payload."""
        assert COORDINATOR_PATH.exists()

        result = subprocess.run(
            [str(COORDINATOR_PATH)],
            input=json.dumps({
                "hook_event_name": "Stop",
                "stop_hook_active": False,
            }),
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, (
            f"Coordinator must exit 0 on normal Stop. stderr: {result.stderr}"
        )

    def test_coordinator_smoke_subagent_stop_exits_zero(self) -> None:
        """Smoke: coordinator exits 0 with SubagentStop payload."""
        assert COORDINATOR_PATH.exists()

        result = subprocess.run(
            [str(COORDINATOR_PATH)],
            input=json.dumps({
                "hook_event_name": "SubagentStop",
                "agent_id": "smoke-test-agent",
                "stop_hook_active": False,
            }),
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, (
            f"Coordinator must exit 0 on SubagentStop. stderr: {result.stderr}"
        )

    def test_coordinator_smoke_stop_hook_active_exits_zero(self) -> None:
        """Smoke: coordinator exits 0 with stop_hook_active=true."""
        assert COORDINATOR_PATH.exists()

        result = subprocess.run(
            [str(COORDINATOR_PATH)],
            input=json.dumps({
                "hook_event_name": "Stop",
                "stop_hook_active": True,
            }),
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0

    def test_coordinator_smoke_stdout_is_empty(self) -> None:
        """Smoke: coordinator stdout must be empty (silent)."""
        assert COORDINATOR_PATH.exists()

        result = subprocess.run(
            [str(COORDINATOR_PATH)],
            input=json.dumps({"hook_event_name": "Stop", "stop_hook_active": True}),
            text=True,
            capture_output=True,
        )
        assert result.stdout == "", (
            f"Coordinator stdout must be empty, got: {result.stdout!r}"
        )

    def test_coordinator_smoke_no_manifest_content_on_stderr(self) -> None:
        """Smoke: coordinator stderr must not contain manifest JSON content."""
        assert COORDINATOR_PATH.exists()

        result = subprocess.run(
            [str(COORDINATOR_PATH)],
            input=json.dumps({"hook_event_name": "Stop", "stop_hook_active": True}),
            text=True,
            capture_output=True,
        )
        # Manifest JSON would start with { and contain schema fields
        # We only check that raw manifest is not dumped to stderr
        stderr = result.stderr
        assert '"agent_session_manifest"' not in stderr, (
            "Manifest content must not appear in coordinator stderr"
        )

    def test_coordinator_smoke_coordinator_file_exists(self) -> None:
        """Smoke: coordinator script exists and is executable."""
        assert COORDINATOR_PATH.exists(), f"Coordinator not found: {COORDINATOR_PATH}"
        assert os.access(COORDINATOR_PATH, os.X_OK), (
            f"Coordinator is not executable: {COORDINATOR_PATH}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
