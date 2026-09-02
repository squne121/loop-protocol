"""Issue #2467 AC7: an e2e fixture shaped like #2432 (10 Acceptance
Criteria: 9 regression-gate VCs + 1 runtime_only VC) proves that the REAL
`baseline_vc_preflight.py` producer's canonical runtime_only current-head
envelope feeds through `adjudicate_vc_result.py` and opens the Step 4 gate
(`evaluate_step4_vc_gate()`) only when the current-head independent binding
is genuinely established -- and that a baseline-only run and a tampered
(malformed) envelope both stay blocked.

Nothing here is a hand-written snapshot/diff: every producer output and the
diff summary are generated from a real git repository, mirroring the
existing E2E fixtures in test_adjudicate_vc_result_non_regression_gate.py.
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
    "adjudicate_vc_result_e2e_runtime_only", ADJUDICATE_SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


# 9 regression-gate ACs (AC1-AC9) + 1 runtime_only AC (AC10) -- shaped like
# #2432's ~10-AC contract (Issue #2467 AC7).
_WORDS = [f"needle{i}" for i in range(1, 10)]


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)


def _commit_all(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()


def _run_producer(
    *,
    repo: Path,
    body_file: Path,
    evidence_mode: str | None = None,
    reviewed_head_sha: str | None = None,
) -> dict:
    argv = [
        sys.executable,
        str(PRODUCER_SCRIPT_PATH),
        "--body-file",
        str(body_file),
        "--cwd",
        str(repo),
        "--format",
        "json",
        "--issue",
        "2467",
        "--repo",
        "squne121/loop-protocol",
    ]
    if evidence_mode is not None:
        argv += ["--evidence-mode", evidence_mode]
    if reviewed_head_sha is not None:
        argv += ["--reviewed-head-sha", reviewed_head_sha]
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert completed.stdout, f"producer emitted no stdout: {completed.stderr}"
    return json.loads(completed.stdout)


def _git_diff_changed_paths(repo: Path, base_sha: str, head_sha: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--name-only", base_sha, head_sha],
        text=True,
    )
    return [line for line in output.splitlines() if line.strip()]


def _body_text() -> str:
    lines = ["## Allowed Paths", "- tracked.txt", "", "## Verification Commands", "", "```bash"]
    for idx, word in enumerate(_WORDS, start=1):
        lines.append(f"# AC{idx}")
        lines.append(f"$ rg -q {word} tracked.txt")
        lines.append("")
    lines.append("# AC10")
    lines.append("# preflight-scope: runtime_only")
    lines.append("$ echo runtime-check")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _make_fixture_repo(tmp_path: Path) -> tuple[Path, Path, str, str, dict]:
    """Build the shared baseline -> implementation commit pair and return
    (repo, body_file, base_sha, head_sha, baseline_result). The REAL baseline
    producer snapshot is captured while the working tree is still AT the
    baseline commit (before the implementation commit is created), matching
    the existing E2E fixtures in test_adjudicate_vc_result_non_regression_gate.py."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    body_file = tmp_path / "issue-2467-body.md"
    body_file.write_text(_body_text(), encoding="utf-8")

    (repo / "tracked.txt").write_text("placeholder\n", encoding="utf-8")
    base_sha = _commit_all(repo, "baseline")

    baseline_result = _run_producer(repo=repo, body_file=body_file)
    for entry in baseline_result["results"][:9]:
        assert entry["classification"] == "expected_fail"
    assert baseline_result["results"][9]["scope_class"] == "runtime_only"

    (repo / "tracked.txt").write_text(" ".join(_WORDS) + "\n", encoding="utf-8")
    head_sha = _commit_all(repo, "implement AC1-AC10")
    return repo, body_file, base_sha, head_sha, baseline_result


def _contract_snapshot(baseline_result: dict) -> dict:
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": baseline_result["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": baseline_result["results"]}},
    }


def _expected_command_hashes(adjudication_result: dict) -> list[str]:
    return [entry["command_hash"] for entry in adjudication_result["per_ac"]]


# --- AC7 positive path -------------------------------------------------


def test_e2e_2432_shaped_fixture_runtime_only_ac_opens_step4_gate_only_with_current_head_binding(
    tmp_path: Path,
) -> None:
    """GIVEN a real 10-AC (#2432-shaped) fixture where 9 ACs are ordinary
    regression-gate VCs and 1 AC (AC10) carries the canonical runtime_only
    producer-skip envelope
    WHEN the real baseline snapshot and a real current-head producer run
    (bound to the actual implementation commit, actual git diff, and actual
    Issue body digest) are fed into adjudicate_vc_result()
    THEN the whole adjudication is nonblocking PASS, AC10 resolves via
    runtime_only_current_head_binding_pass, and evaluate_step4_vc_gate()
    opens (invoke_pr_reviewer is True) for this exact current-head binding
    tuple."""
    repo, body_file, base_sha, head_sha, baseline_result = _make_fixture_repo(tmp_path)

    runtime_only_baseline_item = baseline_result["results"][9]
    assert runtime_only_baseline_item["scope_class"] == "runtime_only"
    assert runtime_only_baseline_item["category"] == "preflight_scope_runtime_only"
    assert runtime_only_baseline_item["runtime_verification_required"] is True

    current_vc_result = _run_producer(
        repo=repo,
        body_file=body_file,
        evidence_mode="current-head",
        reviewed_head_sha=head_sha,
    )
    assert current_vc_result["status"] == "pass"

    changed_paths = _git_diff_changed_paths(repo, base_sha, head_sha)
    assert changed_paths == ["tracked.txt"]
    diff_summary = {
        "changed_paths": changed_paths,
        "head_sha": head_sha,
        "pr_number": 2469,
    }

    contract_snapshot = _contract_snapshot(baseline_result)

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    # AC10 (runtime_only) is a producer-authorized skip mixed with 9 real
    # executed ACs; per pr_review_only precedent (Issue #1540/#88
    # non-regression), a skip mixed with real per_ac entries is excluded
    # from the per_ac list itself (it never fails or blocks) rather than
    # appearing alongside them.
    assert len(result["per_ac"]) == 9
    assert {entry["ac"] for entry in result["per_ac"]} == {f"AC{i}" for i in range(1, 10)}
    for entry in result["per_ac"]:
        assert entry["reason_code"] == "expected_fail_resolved_on_current_head"

    gate = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha=head_sha,
        expected_contract_body_sha256=contract_snapshot["body_sha256"],
        expected_command_hashes=_expected_command_hashes(result),
    )
    assert gate["invoke_pr_reviewer"] is True
    assert gate["reason_code"] is None

    # Isolate AC10 alone (the runtime_only-only shape) to exercise the
    # dedicated runtime_only_current_head_binding_pass synthesis path and
    # confirm it independently opens the Step 4 gate for its own binding
    # tuple.
    runtime_only_only_snapshot = {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": baseline_result["source"]["body_sha256"],
        "checks": {"vc_preflight": {"classifications": [runtime_only_baseline_item]}},
    }
    runtime_only_only_current = dict(current_vc_result)
    runtime_only_only_current["results"] = [current_vc_result["results"][9]]

    isolated_result = mod.adjudicate_vc_result(
        contract_snapshot=runtime_only_only_snapshot,
        current_vc_result=runtime_only_only_current,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
    )

    assert isolated_result["overall_status"] == "pass"
    assert isolated_result["blocking"] is False
    assert len(isolated_result["per_ac"]) == 1
    assert isolated_result["per_ac"][0]["ac"] == "AC10"
    assert isolated_result["per_ac"][0]["reason_code"] == "runtime_only_current_head_binding_pass"

    isolated_gate = mod.evaluate_step4_vc_gate(
        isolated_result,
        expected_head_sha=head_sha,
        expected_contract_body_sha256=runtime_only_only_snapshot["body_sha256"],
        expected_command_hashes=_expected_command_hashes(isolated_result),
    )
    assert isolated_gate["invoke_pr_reviewer"] is True
    assert isolated_gate["reason_code"] is None


# --- AC7 negative path 1: baseline-only never opens the gate ------------


def test_e2e_baseline_only_run_never_opens_step4_gate(tmp_path: Path) -> None:
    """GIVEN the SAME real fixture, but "current" is actually the producer's
    default BASELINE-mode run (not a certified current-head run)
    WHEN fed into adjudicate_vc_result()
    THEN the adjudication is never nonblocking PASS and the Step 4 gate
    never opens (a baseline skip declaration alone is insufficient)."""
    repo, body_file, _base_sha, _head_sha, baseline_result = _make_fixture_repo(tmp_path)

    contract_snapshot = _contract_snapshot(baseline_result)

    # Re-run the producer in baseline (non current-head) mode over the exact
    # same (already-implemented) tree -- this is the actual current_vc_result
    # payload the SubAgent would see if it fabricated a "current" run without
    # a real current-head binding: its own top-level status is not "pass".
    baseline_only_current = _run_producer(repo=repo, body_file=body_file)
    assert baseline_only_current["status"] != "pass"

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=baseline_only_current,
        diff_summary={"changed_paths": ["tracked.txt"], "head_sha": None, "pr_number": 2469},
        allowed_paths=["tracked.txt"],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True

    gate = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha="does-not-matter",
        expected_contract_body_sha256=contract_snapshot["body_sha256"],
        expected_command_hashes=[],
    )
    assert gate["invoke_pr_reviewer"] is False


# --- AC7 negative path 2: a tampered/malformed envelope never opens the gate


def test_e2e_tampered_runtime_only_envelope_never_opens_step4_gate(tmp_path: Path) -> None:
    """GIVEN the SAME real, correctly-bound current-head fixture, but AC10's
    current envelope has been tampered with (verification_owner rewritten
    away from the producer's canonical value -- a malformed/forged envelope)
    WHEN fed into adjudicate_vc_result()
    THEN the tampered AC is rejected via a fail-closed authorization
    mismatch and the Step 4 gate never opens."""
    repo, body_file, base_sha, head_sha, baseline_result = _make_fixture_repo(tmp_path)

    contract_snapshot = _contract_snapshot(baseline_result)

    current_vc_result = _run_producer(
        repo=repo,
        body_file=body_file,
        evidence_mode="current-head",
        reviewed_head_sha=head_sha,
    )
    assert current_vc_result["status"] == "pass"
    # Tamper with AC10's current envelope only (baseline stays canonical).
    current_vc_result["results"][9]["verification_owner"] = "attacker-controlled"

    changed_paths = _git_diff_changed_paths(repo, base_sha, head_sha)
    diff_summary = {"changed_paths": changed_paths, "head_sha": head_sha, "pr_number": 2469}

    result = mod.adjudicate_vc_result(
        contract_snapshot=contract_snapshot,
        current_vc_result=current_vc_result,
        diff_summary=diff_summary,
        allowed_paths=["tracked.txt"],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_authorization_mismatch:AC10"]

    gate = mod.evaluate_step4_vc_gate(
        result,
        expected_head_sha=head_sha,
        expected_contract_body_sha256=contract_snapshot["body_sha256"],
        expected_command_hashes=[],
    )
    assert gate["invoke_pr_reviewer"] is False
