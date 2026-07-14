#!/usr/bin/env python3
"""
Unit tests for Issue #1338 AC1-AC3: same normalized command dedup/replay in
baseline_vc_preflight.py.
"""

import json
import shutil
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


# ---------------------------------------------------------------------------
# P0-1 (PR #1508 review): state-epoch barrier tests.
# ---------------------------------------------------------------------------


def _reset_state_probe_fixture(fixture_dir: Path, probe_path: Path) -> None:
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    fixture_dir.mkdir()
    probe_path.write_text("x = 1\n", encoding="utf-8")


_STATE_EPOCH_BODY = """## Verification Commands

```bash
# AC1
$ test -d fixture/__pycache__
# AC2
$ python3 -m py_compile fixture/state_probe.py
# AC3
$ test -d fixture/__pycache__
```
"""


def test_p0_1_state_epoch_barrier_prevents_stale_dedup_replay(tmp_path, monkeypatch, capsys):
    """P0-1: `test -d fixture/__pycache__` executed BEFORE and AFTER a
    stateful `python3 -m py_compile fixture/state_probe.py` barrier must be
    re-executed fresh each time (fixing AC3 against the real post-barrier
    state), not dedup-replayed against AC1's stale pre-barrier snapshot.
    Holds identically at --max-workers 1 and 2."""
    fixture_dir = tmp_path / "fixture"
    probe = fixture_dir / "state_probe.py"
    body_file = tmp_path / "issue_body.md"
    body_file.write_text(_STATE_EPOCH_BODY, encoding="utf-8")

    results_by_workers = {}
    for max_workers in ("1", "2"):
        _reset_state_probe_fixture(fixture_dir, probe)
        _, data = _run_main(
            monkeypatch,
            capsys,
            [
                "--body-file", str(body_file), "--issue", "999", "--cwd", str(tmp_path),
                "--max-workers", max_workers,
            ],
        )
        results_by_workers[max_workers] = data["results"]

    for max_workers, results in results_by_workers.items():
        assert len(results) == 3, max_workers
        ac1, ac2, ac3 = results
        # AC1: __pycache__ does not exist yet -> `test -d` fails (exit 1)
        assert ac1["exit_code"] == 1, max_workers
        # AC2: py_compile succeeds and creates __pycache__ as a side effect.
        assert ac2["exit_code"] == 0, max_workers
        # AC3: __pycache__ now exists -> `test -d` succeeds (exit 0). If AC3
        # had been (incorrectly) dedup-replayed against AC1's pre-barrier
        # snapshot, this would still read exit_code == 1.
        assert ac3["exit_code"] == 0, max_workers
        # AC1 and AC3 share identical raw command text ...
        assert ac1["raw_command"] == ac3["raw_command"]
        # ... but MUST NOT share an execution_key_hash (different state
        # epoch) and AC3 must not be flagged as a dedup replay of AC1.
        assert ac1["execution_key_hash"] != ac3["execution_key_hash"], max_workers
        assert ac3["runner"] != "dedup_replay", max_workers
        assert "dedup" not in ac3 or ac3.get("dedup") is None, max_workers

    # Same fixed-point exit codes at both --max-workers 1 and 2.
    assert [r["exit_code"] for r in results_by_workers["1"]] == [
        r["exit_code"] for r in results_by_workers["2"]
    ]


def test_p0_1_execution_key_hash_changes_with_state_epoch():
    """compute_execution_key_hash() must produce a distinct hash for the
    same argv/cwd/env/timeout when state_epoch differs (P0-1)."""
    argv = ["test", "-d", "fixture/__pycache__"]
    epoch_0 = vcp.compute_execution_key_hash(argv, "/tmp/repo", {}, 90, state_epoch=0)
    epoch_0_again = vcp.compute_execution_key_hash(argv, "/tmp/repo", {}, 90, state_epoch=0)
    epoch_1 = vcp.compute_execution_key_hash(argv, "/tmp/repo", {}, 90, state_epoch=1)

    assert epoch_0 == epoch_0_again
    assert epoch_0 != epoch_1


# ---------------------------------------------------------------------------
# P2-2 (PR #1508 review): dedup provenance identifies the exact source AC.
# ---------------------------------------------------------------------------


def test_p2_2_dedup_provenance_identifies_source_result_index_ac_and_line(tmp_path, monkeypatch, capsys):
    body_file = _write_body(tmp_path, _DEDUP_BODY)

    _, data = _run_main(
        monkeypatch,
        capsys,
        ["--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT)],
    )

    source, replay = data["results"][0], data["results"][1]
    assert replay["dedup"]["source_result_index"] == 0
    assert replay["dedup"]["source_ac"] == source["ac"] == "AC1"
    assert replay["dedup"]["source_line"] == source["line"]
    assert replay["dedup"]["source_line"] != replay["line"]
