"""
Issue #1488 AC5: boundary test proving the REAL producer
(`baseline_vc_preflight.py`) current-head envelope for a non-regression-gate
VC (rg) feeds into `adjudicate_vc_result.py` and resolves a baseline
`expected_fail` into `expected_fail_resolved_on_current_head`.

This deliberately does NOT rely on a hand-written fixture for the
`current_vc_result` payload (fixture-only false-green prevention, AC5):
the current-head envelope is produced by actually invoking
`baseline_vc_preflight.py` as a subprocess against a real temporary git
repository.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
ADJUDICATE_SCRIPT_PATH = (
    ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "adjudicate_vc_result.py"
)
PRODUCER_SCRIPT_PATH = (
    ROOT
    / ".claude"
    / "skills"
    / "issue-contract-review"
    / "scripts"
    / "baseline_vc_preflight.py"
)

_spec = importlib.util.spec_from_file_location(
    "adjudicate_vc_result_non_regression_gate", ADJUDICATE_SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)


def _run_producer_current_head(
    *, repo: Path, body_file: Path, reviewed_head_sha: str
) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            str(PRODUCER_SCRIPT_PATH),
            "--body-file",
            str(body_file),
            "--cwd",
            str(repo),
            "--evidence-mode",
            "current-head",
            "--reviewed-head-sha",
            reviewed_head_sha,
            "--format",
            "json",
            "--issue",
            "1488",
            "--repo",
            "squne121/loop-protocol",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.stdout, f"producer emitted no stdout: {completed.stderr}"
    return json.loads(completed.stdout)


def test_real_producer_non_regression_gate_current_pass_resolves_baseline_expected_fail(
    tmp_path: Path,
) -> None:
    """GIVEN a real baseline_vc_preflight.py current-head run over a non
    regression-gate rg VC that now matches (exit 0) WHEN its output is fed to
    adjudicate_vc_result() with a baseline expected_fail snapshot THEN the
    overall status is pass and the AC resolves via
    expected_fail_resolved_on_current_head (producer-certified, not a
    hand-written fixture)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()

    body = (
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "$ rg -q hello tracked.txt\n"
        "```\n"
    )
    # body_file lives OUTSIDE the repo so writing it does not dirty the
    # worktree under test.
    body_file = tmp_path / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    current_vc_result = _run_producer_current_head(
        repo=repo, body_file=body_file, reviewed_head_sha=head
    )

    # Sanity: the real producer must have certified this as a current PASS
    # envelope (Issue #1488's actual fix under test), not merely exited 0.
    assert current_vc_result["status"] == "pass"
    assert current_vc_result["errors"] == []
    assert current_vc_result["fallback_detected"] is False
    assert current_vc_result["human_review_required"] is False
    assert current_vc_result["stop_condition_triggered"] is False
    assert len(current_vc_result["results"]) == 1
    current_item = current_vc_result["results"][0]
    assert current_item["classification"] == "expected_pass"
    assert current_item["category"] == "expected_pass_resolved_on_current_head"
    assert current_item["exit_code"] == 0

    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": current_vc_result["source"]["body_sha256"],
        "checks": {
            "vc_preflight": {
                "classifications": [
                    {
                        "ac": current_item["ac"],
                        "command_hash": current_item["command_hash"],
                        "exit_code": 1,
                        "category": "expected_baseline_fail",
                        "classification": "expected_fail",
                        "decision": "go",
                        "raw_command": current_item["raw_command"],
                    }
                ]
            }
        },
    }
    diff_summary = {
        "changed_paths": ["tracked.txt"],
        "head_sha": head,
    }
    allowed_paths = ["tracked.txt"]

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    assert len(result["per_ac"]) == 1
    assert result["per_ac"][0]["reason_code"] == "expected_fail_resolved_on_current_head"


def test_real_producer_non_regression_gate_test_f_current_pass_resolves_baseline_expected_fail(
    tmp_path: Path,
) -> None:
    """GIVEN a real baseline_vc_preflight.py current-head run over a
    non-regression-gate `test -f` VC that now succeeds (the file was created
    by the implementation) WHEN fed into adjudicate_vc_result() THEN the
    overall status is pass with expected_fail_resolved_on_current_head."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "implemented.txt").write_text("done\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "implemented.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()

    body = (
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "$ test -f implemented.txt\n"
        "```\n"
    )
    body_file = tmp_path / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    current_vc_result = _run_producer_current_head(
        repo=repo, body_file=body_file, reviewed_head_sha=head
    )

    assert current_vc_result["status"] == "pass"
    current_item = current_vc_result["results"][0]
    assert current_item["classification"] == "expected_pass"
    assert current_item["category"] == "expected_pass_resolved_on_current_head"

    contract_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": current_vc_result["source"]["body_sha256"],
        "checks": {
            "vc_preflight": {
                "classifications": [
                    {
                        "ac": current_item["ac"],
                        "command_hash": current_item["command_hash"],
                        "exit_code": 1,
                        "category": "file_not_found_expected",
                        "classification": "expected_fail",
                        "decision": "go",
                        "raw_command": current_item["raw_command"],
                    }
                ]
            }
        },
    }
    diff_summary = {"changed_paths": ["implemented.txt"], "head_sha": head}
    allowed_paths = ["implemented.txt"]

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=allowed_paths,
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    assert result["per_ac"][0]["reason_code"] == "expected_fail_resolved_on_current_head"


def test_real_producer_baseline_mode_non_regression_gate_pass_stays_uncertified(
    tmp_path: Path,
) -> None:
    """GIVEN a real baseline_vc_preflight.py run in the DEFAULT baseline
    evidence-mode (not current-head) over the same rg VC that exits 0 THEN
    the producer envelope status is NOT pass, so adjudicate_vc_result() must
    never certify a current pass from it (AC1 cross-check via consumer)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)

    body = (
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "$ rg -q hello tracked.txt\n"
        "```\n"
    )
    body_file = tmp_path / "issue-body.md"
    body_file.write_text(body, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(PRODUCER_SCRIPT_PATH),
            "--body-file",
            str(body_file),
            "--issue",
            "1488",
            "--repo",
            "squne121/loop-protocol",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo),
    )
    current_vc_result = json.loads(completed.stdout)

    # AC1: baseline mode must NOT certify a non-regression-gate exit 0 VC.
    assert current_vc_result["status"] != "pass"
    assert current_vc_result["results"][0]["classification"] == "unexpected_pass"
