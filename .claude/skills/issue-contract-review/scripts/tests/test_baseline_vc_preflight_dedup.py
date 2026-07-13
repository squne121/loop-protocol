#!/usr/bin/env python3
"""
Unit tests for Issue #1338 AC1-AC3: same normalized command dedup/replay in
baseline_vc_preflight.py.
"""

import json
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import baseline_vc_preflight as vcp  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[5]
_EXISTING_TARGET = ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py"

_DEDUP_BODY = f"""## Verification Commands

```bash
# AC1
$ rg -q nonexistent_pattern_dedup_1338 {_EXISTING_TARGET}
# AC2
# baseline-expect: pass
$ rg -q nonexistent_pattern_dedup_1338 {_EXISTING_TARGET}
```
"""


def _run_main(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["baseline_vc_preflight.py", *argv])
    exit_code = vcp.main()
    captured = capsys.readouterr()
    return exit_code, json.loads(captured.out)


def _write_body(tmp_path, text):
    body_file = tmp_path / "issue_body.md"
    body_file.write_text(text, encoding="utf-8")
    return body_file


def test_ac1_identical_normalized_command_executes_subprocess_only_once(tmp_path, monkeypatch, capsys):
    """AC1: two ACs referencing the same normalized command (same argv/cwd/env/
    timeout) launch the real subprocess exactly once; the 2nd result is a
    dedup_replay."""
    body_file = _write_body(tmp_path, _DEDUP_BODY)

    call_count = {"n": 0}
    original_run_command = vcp.run_command

    def counting_run_command(command, timeout_seconds, cwd):
        call_count["n"] += 1
        return original_run_command(command, timeout_seconds, cwd)

    monkeypatch.setattr(vcp, "run_command", counting_run_command)

    _, data = _run_main(
        monkeypatch,
        capsys,
        ["--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT)],
    )

    assert call_count["n"] == 1, "expected exactly one real subprocess execution for the dedup pair"
    results = data["results"]
    assert len(results) == 2
    assert results[0]["ac"] == "AC1"
    assert results[1]["ac"] == "AC2"
    assert results[0]["runner"] == "exec"
    assert results[1]["runner"] == "dedup_replay"


def test_ac2_dedup_replay_recomputes_classification_per_ac_and_keeps_own_identity(tmp_path, monkeypatch, capsys):
    """AC2: dedup_replay keeps its own ac/line/raw_command, and classification/
    decision are recomputed from THAT AC's own annotations (baseline-expect:
    pass here), not copied from the source AC's result."""
    body_file = _write_body(tmp_path, _DEDUP_BODY)

    _, data = _run_main(
        monkeypatch,
        capsys,
        ["--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT)],
    )

    results = data["results"]
    source, replay = results[0], results[1]

    # Both share the same raw command text (execution conditions identical).
    assert source["raw_command"] == replay["raw_command"]
    assert source["line"] != replay["line"]

    # Shared execution outcome fields must match exactly (same subprocess result).
    assert source["exit_code"] == replay["exit_code"]
    assert source["stdout_head"] == replay["stdout_head"]
    assert source["runner_env_delta"] == replay["runner_env_delta"]
    assert source["execution_key_hash"] == replay["execution_key_hash"]

    # classification/decision MUST differ: AC1 has no baseline-expect annotation
    # (plain expected_fail/go for rg exit 1), AC2 has baseline-expect: pass with
    # a non-zero exit -> baseline_regression_failed/human_judgment. If the
    # implementation had copied AC1's classification onto AC2, this would fail.
    assert source["classification"] == "expected_fail"
    assert source["decision"] == "go"
    assert replay["classification"] == "human_judgment"
    assert replay["category"] == "baseline_regression_failed"
    assert replay["decision"] == "human_judgment"

    # dedup provenance object present only on the replay entry.
    assert "dedup" not in source or source.get("dedup") is None
    assert replay["dedup"]["source_command_hash"] == source["command_hash"]
    assert replay["dedup"]["source_execution_key_hash"] == source["execution_key_hash"]


def test_ac3_execution_key_hash_is_distinct_field_and_covers_cwd_env_timeout():
    """AC3: execution_key_hash is a dedup key separate from command_hash (raw
    command text hash) and changes if cwd / runner_env_delta / timeout_seconds
    differ, even when argv is identical."""
    argv = ["rg", "-q", "foo", "bar.py"]

    base = vcp.compute_execution_key_hash(argv, "/tmp/repo-a", {}, 90)
    same_again = vcp.compute_execution_key_hash(argv, "/tmp/repo-a", {}, 90)
    different_cwd = vcp.compute_execution_key_hash(argv, "/tmp/repo-b", {}, 90)
    different_env = vcp.compute_execution_key_hash(argv, "/tmp/repo-a", {"CI": "true"}, 90)
    different_timeout = vcp.compute_execution_key_hash(argv, "/tmp/repo-a", {}, 30)

    assert base == same_again
    assert base != different_cwd
    assert base != different_env
    assert base != different_timeout

    # A raw command hash (command_hash) is a distinct concept/value.
    command_hash = vcp.compute_command_hash("rg -q foo bar.py")
    assert command_hash != base


def test_ac3_result_item_exposes_both_command_hash_and_execution_key_hash(tmp_path, monkeypatch, capsys):
    """AC3: an executed result exposes both command_hash and execution_key_hash
    as separate fields."""
    body_file = _write_body(tmp_path, _DEDUP_BODY)

    _, data = _run_main(
        monkeypatch,
        capsys,
        ["--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT)],
    )

    result = data["results"][0]
    assert result["command_hash"].startswith("sha256:")
    assert result["execution_key_hash"].startswith("sha256:")
    assert result["command_hash"] != result["execution_key_hash"]
