"""Runtime coverage for the Linux-only SubAgent launch ledger writer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRITER_SOURCE = ROOT / "scripts" / "subagent-launch-ledger-writer.c"
HOOK = ROOT / "scripts" / "check-codex-agents.mjs"
VALIDATOR = ROOT / "scripts" / "check_subagent_launch_ledger.py"
PYTHON_TEST_PLAN = ROOT / ".github" / "ci" / "python-test-plan.json"


def build_writer(tmp_path: Path) -> Path:
    binary = tmp_path / "ledger-writer"
    subprocess.run(
        ["cc", "-std=c17", "-Wall", "-Wextra", "-Werror", "-O2", "-o", str(binary), str(WRITER_SOURCE)],
        check=True,
        text=True,
        capture_output=True,
    )
    return binary


def entry(name: str) -> dict[str, object]:
    return {
        "agent_name": name,
        "event_type": "SubagentStart",
        "evidence_source": "event_derived",
        "event_fingerprint": f"fingerprint-{name}",
        "declared_runtime": {
            "model": "gpt-5.3-codex-spark",
            "reasoning_effort": "medium",
            "default_permissions": "loop-protocol-readonly",
            "agent_definition_sha256": "a" * 64,
        },
        "observed_dispatch": {
            "model": "gpt-5.3-codex-spark",
            "session_id": "session",
            "turn_id": "turn",
            "agent_id": name,
            "observed_at": "2026-07-16T00:00:00Z",
        },
        "correlation": {
            "evidence_run_id": "run",
            "repo_head_sha": "a" * 40,
            "worktree_dirty": False,
        },
    }


def invoke(writer: Path, repo: Path, launch: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(writer), "--repo", str(repo), "--kind", "launches", "--entry", json.dumps(launch), "--identity", launch["event_fingerprint"]],
        text=True,
        capture_output=True,
    )


def test_independent_trusted_processes_preserve_distinct_evidence(tmp_path: Path):
    writer = build_writer(tmp_path)
    commands = [
        [str(writer), "--repo", str(tmp_path), "--kind", "launches", "--entry", json.dumps(entry("spark-skim")), "--identity", "fingerprint-spark-skim"],
        [str(writer), "--repo", str(tmp_path), "--kind", "launches", "--entry", json.dumps(entry("spark-deep")), "--identity", "fingerprint-spark-deep"],
    ]
    first = subprocess.Popen(commands[0], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen(commands[1], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert first.wait(timeout=10) == 0
    assert second.wait(timeout=10) == 0
    ledger = json.loads((tmp_path / "artifacts/codex/subagent-launch-ledger.json").read_text())
    assert {item["event_fingerprint"] for item in ledger["launches"]} == {
        "fingerprint-spark-skim",
        "fingerprint-spark-deep",
    }


def test_hook_builds_writer_and_records_dispatch_evidence(tmp_path: Path):
    agent_dir = tmp_path / ".codex" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "spark-skim.toml").write_text(
        "model = \"gpt-5.3-codex-spark\"\nmodel_reasoning_effort = \"medium\"\ndefault_permissions = \"loop-protocol-readonly\"\n",
        encoding="utf-8",
    )
    writer_dir = tmp_path / "scripts"
    writer_dir.mkdir()
    shutil.copy2(WRITER_SOURCE, writer_dir / WRITER_SOURCE.name)
    payload = {
        "agent_type": "spark-skim",
        "model": "gpt-5.3-codex-spark",
        "session_id": "session",
        "turn_id": "turn",
        "agent_id": "agent",
    }
    result = subprocess.run(
        ["node", str(HOOK), "--hook-subagent-start"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "REPO_ROOT_OVERRIDE": str(tmp_path),
            "CODEX_AGENT_EVIDENCE_RUN_ID": "run",
            "CODEX_AGENT_EVIDENCE_HEAD_SHA": "a" * 40,
        },
    )
    assert result.returncode == 0, result.stderr
    ledger = json.loads((tmp_path / "artifacts/codex/subagent-launch-ledger.json").read_text())
    assert ledger["launches"][0]["observed_dispatch"]["agent_id"] == "agent"


def test_preexisting_substitution_and_nonregular_entries_fail_closed(tmp_path: Path):
    writer = build_writer(tmp_path)
    parent_link = tmp_path / "artifacts"
    parent_link.symlink_to(tmp_path / "outside")
    assert invoke(writer, tmp_path, entry("spark-skim")).returncode != 0

    for name, create in (
        ("subagent-launch-ledger.json", lambda path: path.symlink_to(tmp_path / "outside")),
        ("subagent-launch-ledger.json.lock", lambda path: os.mkfifo(path)),
        ("subagent-launch-ledger.json.tmp", lambda path: os.mkfifo(path)),
    ):
        case_root = tmp_path / name.replace(".", "-")
        ledger_dir = case_root / "artifacts" / "codex"
        ledger_dir.mkdir(parents=True)
        target = ledger_dir / name
        create(target)
        result = invoke(writer, case_root, entry("spark-skim"))
        assert result.returncode != 0
        assert target.exists() or target.is_symlink()


def test_malformed_and_replacement_failures_never_reset_or_publish_partial_json(tmp_path: Path):
    writer = build_writer(tmp_path)
    ledger_dir = tmp_path / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    ledger = ledger_dir / "subagent-launch-ledger.json"
    original = b'{"ledger_schema":'
    ledger.write_bytes(original)
    result = invoke(writer, tmp_path, entry("spark-skim"))
    assert result.returncode != 0
    assert ledger.read_bytes() == original
    assert not (ledger_dir / "subagent-launch-ledger.json.tmp").exists()

    ledger.unlink()
    residue = ledger_dir / "subagent-launch-ledger.json.tmp"
    residue.write_text("do-not-replace", encoding="utf-8")
    result = invoke(writer, tmp_path, entry("spark-skim"))
    assert result.returncode != 0
    assert residue.read_text(encoding="utf-8") == "do-not-replace"
    assert not ledger.exists()


def test_schema_invalid_ledger_missing_coverage_scope_fails_closed_without_replacement(tmp_path: Path):
    writer = build_writer(tmp_path)
    ledger_dir = tmp_path / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    ledger = ledger_dir / "subagent-launch-ledger.json"
    original = json.dumps({
        "ledger_schema": "SUBAGENT_LAUNCH_LEDGER_V1",
        "generated_by": "codex_hook_pipeline",
        "launches": [],
        "root_thread_actions": [],
    }).encode()
    ledger.write_bytes(original)

    result = invoke(writer, tmp_path, entry("spark-skim"))

    assert result.returncode != 0
    assert "ledger_parse_or_schema_invalid" in result.stderr
    assert ledger.read_bytes() == original
    assert not (ledger_dir / "subagent-launch-ledger.json.tmp").exists()


def test_canonical_evidence_requires_launch_dispatch_and_correlation(tmp_path: Path):
    runtime = json.loads(
        (ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json").read_text(encoding="utf-8")
    )["required_agents"]["spark-skim"]
    payload = {
        "ledger_schema": "SUBAGENT_LAUNCH_LEDGER_V1",
        "generated_by": "codex_hook_pipeline",
        "coverage_scope": {
            "subagent_start_event_recorded": True,
            "supported_pretooluse_paths": ["Bash", "apply_patch", "Edit", "Write"],
            "unsupported_paths_fail_closed": True,
            "scope_note": "supported PreToolUse paths only",
        },
        "launches": [{
            "agent_name": "spark-skim",
            "event_type": "SubagentStart",
            "evidence_source": "event_derived",
            "event_fingerprint": "declared-only",
            "runtime": {
                "model": runtime["model"],
                "reasoning_effort": runtime["model_reasoning_effort"],
                "default_permissions": runtime["default_permissions"],
            },
        }],
        "root_thread_actions": [],
    }
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run([sys.executable, str(VALIDATOR), "--audit-mode", str(ledger)], text=True, capture_output=True)
    assert result.returncode == 1
    assert "dispatch_evidence_missing" in json.loads(result.stdout)["error_codes"]


def test_ssot_documents_native_writer_boundary():
    text = (ROOT / "docs/dev/agent-skill-boundaries.md").read_text(encoding="utf-8")
    assert "subagent-launch-ledger-writer.c" in text
    assert "hostile process" in text


def test_ci_test_selection_plan_includes_writer_test_once():
    plan = json.loads(PYTHON_TEST_PLAN.read_text(encoding="utf-8"))
    targets = plan["targets"]
    assert targets.count("tests/test_subagent_launch_ledger_writer.py") == 1
    assert targets.index("tests/test_subagent_launch_ledger_writer.py") == (
        targets.index("tests/test_subagent_launch_ledger.py") + 1
    )
