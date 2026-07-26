"""Runtime probe regression tests; fixture execution never becomes runtime PASS."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE = REPO_ROOT / ".claude/scripts/run_issue_reviewer_runtime_probe.py"
COLLECTOR = REPO_ROOT / ".claude/scripts/collect_issue_reviewer_runtime_evidence.py"


def test_unavailable_runtime_exits_77_and_writes_skip_without_raw_transcript(tmp_path: Path) -> None:
    environment = os.environ | {"PATH": "", "CLAUDE_CODE_TRUSTED_HOST_PROVENANCE": ""}
    result = subprocess.run(
        [sys.executable, str(PROBE), "--issue", "1754", "--scenario", "allow", "--no-publish", "--artifact-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 77
    assert result.stdout.startswith("SKIP:")
    artifact = next(tmp_path.glob("runtime-probe-*.json"))
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["result"] == "skip"
    assert payload["raw_transcript_persisted"] is False
    assert "prompt" not in artifact.read_text(encoding="utf-8")


def test_collector_treats_skip_as_skip_not_pass(tmp_path: Path) -> None:
    (tmp_path / "runtime-probe-test.json").write_text(
        json.dumps({"schema": "ISSUE_REVIEWER_RUNTIME_PROBE_V1", "issue": 1754, "result": "skip"}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(COLLECTOR), "--issue", "1754", "--emit-test-verdict", "--artifact-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 77
    payload = json.loads(result.stdout)
    assert payload["TEST_VERDICT_MACHINE"]["result"] == "skip"
