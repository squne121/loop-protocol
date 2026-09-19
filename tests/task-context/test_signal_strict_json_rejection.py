"""AC2: strict JSON parser does not coerce invalid public signals."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

import task_context_workflow_signals as signals

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "task-context"
_CLI_PATH = _SCRIPTS_DIR / "task_contextctl.py"


def _state_entries(state_root: pathlib.Path) -> list[str]:
    if not state_root.exists():
        return []
    return sorted(path.relative_to(state_root).as_posix() for path in state_root.rglob("*"))


def test_given_duplicate_evidence_json_member_when_public_signal_is_parsed_then_no_normalized_payload_is_produced():
    payload, outcome = signals.parse_public_signal(
        '{"signal_kind":"implementation_pr_observed","source":"open-pr",'
        '"source_schema_version":"v1","evidence":{"repo":"squne121/loop-protocol",'
        '"issue_number":20,"pr_number":21,"pr_number":22}}'
    )
    assert payload is None
    assert outcome == {"disposition": "rejected_evidence", "reason_code": "DUPLICATE_EVIDENCE_MEMBER"}


@pytest.mark.parametrize(
    ("raw_signal", "reason_code"),
    [
        ('{"signal_kind":', "MALFORMED_JSON"),
        ('["not", "an", "object"]', "NON_OBJECT_ROOT"),
    ],
)
def test_given_invalid_direct_signal_cli_input_when_run_then_rejected_envelope_without_state_mutation(
    state_root, raw_signal, reason_code
):
    """The public direct path, not a helper, owns the frozen taxonomy."""
    before = _state_entries(state_root)
    env = dict(os.environ)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)

    proc = subprocess.run(
        [sys.executable, str(_CLI_PATH), "signal", "apply"],
        input=raw_signal,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line, got: {proc.stdout!r}"
    result = json.loads(lines[0])
    assert result["status"] == "ok"
    assert result["code"] == "OK"
    assert result["data"] == {"disposition": "rejected_envelope", "reason_code": reason_code}
    assert _state_entries(state_root) == before
