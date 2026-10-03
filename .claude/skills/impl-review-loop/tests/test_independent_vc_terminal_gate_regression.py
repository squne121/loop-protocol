"""Issue #2837: independent-VC canonical consumer production wiring regression.

These tests call the PRODUCTION entrypoints only: the `step4-adjudicate`,
`step5-terminal-gate`, and `step4-gate` CLIs through a subprocess, and the
module functions in-process. There is no "correct pseudo-orchestrator" in
this file: "reviewer dispatch count" is defined as the number of
`step4-adjudicate` invocations that returned `invoke` (exit 0), and terminal
approval is the exit code / route of `step5-terminal-gate`.

A gate is forced to block either with a stale / corrupt `loop_state` (CLI) or
by replacing `evaluate_step4_vc_gate` (in-process). No skip / xfail markers
are used anywhere in this file.
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = ROOT / ".claude" / "skills" / "impl-review-loop"
SCRIPT_PATH = SKILL_DIR / "scripts" / "adjudicate_vc_result.py"
STEP2_DOC = SKILL_DIR / "steps" / "step-2-verification.md"
STEP4_DOC = SKILL_DIR / "steps" / "step-4-pr-review.md"
STEP5_DOC = SKILL_DIR / "steps" / "step-5-feedback-and-termination.md"

_spec = importlib.util.spec_from_file_location(
    "adjudicate_vc_result_independent_vc_terminal_gate_regression", SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


HEAD_A = "a" * 40
HEAD_B = "b" * 40
BODY_A = "sha256:" + "1" * 64
BODY_B = "sha256:" + "2" * 64
H_AC1 = "sha256:" + "c" * 64
H_AC2 = "sha256:" + "d" * 64
H_AC3_RUNTIME = "sha256:" + "e" * 64
ISSUE_NUMBER = 2837
PR_NUMBER = 2900
ALLOWED_PATHS = ["src/"]
CHANGED_PATHS = ["src/a.py"]
GENERATED_AT = "2026-10-03T00:00:00Z"


# --- fixture builders -------------------------------------------------------


def _hashes(runtime_only: bool, *, swap_first: bool = False) -> list[str]:
    hashes = [H_AC1, H_AC2] + ([H_AC3_RUNTIME] if runtime_only else [])
    if swap_first:
        hashes[0] = "sha256:" + "9" * 64
    return hashes


def _contract_snapshot(body: str, runtime_only: bool) -> dict[str, Any]:
    results: list[dict[str, Any]] = [
        {"ac": "AC1", "command_hash": H_AC1, "classification": "expected_fail", "exit_code": 1},
        {"ac": "AC2", "command_hash": H_AC2, "classification": "expected_fail", "exit_code": 1},
    ]
    if runtime_only:
        results.append(
            {
                "ac": "AC3",
                "command_hash": H_AC3_RUNTIME,
                "classification": "skipped",
                "runner": "skipped",
                "scope_class": "runtime_only",
                "decision": "go",
                "category": "preflight_scope_runtime_only",
                "verification_owner": "impl-review-loop",
                "deferred_reason": "needs a live runtime",
                "runtime_verification_required": True,
            }
        )
    return {
        "schema": "baseline_vc_preflight/v1",
        "status": "go",
        "body_sha256": body,
        "results": results,
    }


def _ac_result(ac: str, command_hash: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ac": ac,
        "command": f"echo {ac}",
        "command_hash": command_hash,
        "exit_code": 0,
        "status": "pass",
        "fallback_detected": False,
        "artifact_present": "not_required",
        "human_review_required": False,
        "stop_condition_triggered": False,
        "notes": "",
    }
    row.update(overrides)
    return row


def _test_verdict(
    head: str,
    body: str,
    runtime_only: bool,
    *,
    ac_overrides: dict[str, dict[str, Any]] | None = None,
    drop_acs: tuple[str, ...] = (),
    result: str = "PASS",
    generated_at: str | None = GENERATED_AT,
    command_hashes: list[str] | None = None,
) -> dict[str, Any]:
    """The documented producer report: independent execution facts only (no
    GitHub workflow / check run / artifact field)."""
    hashes = command_hashes if command_hashes is not None else _hashes(runtime_only)
    acs = ["AC1", "AC2"] + (["AC3"] if runtime_only else [])
    rows = []
    for ac, command_hash in zip(acs, hashes):
        if ac in drop_acs:
            continue
        rows.append(_ac_result(ac, command_hash, **((ac_overrides or {}).get(ac, {}))))
    report: dict[str, Any] = {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "issue_number": ISSUE_NUMBER,
        "pr_number": PR_NUMBER,
        "head_sha": head,
        "reviewed_head_sha": head,
        "diff_head_sha": head,
        "contract_body_sha256": body,
        "result": result,
        "runtime_ac_results": rows,
    }
    if generated_at is not None:
        report["generated_at"] = generated_at
    return report


def _diff_summary(head: str) -> dict[str, Any]:
    return {"head_sha": head, "pr_number": PR_NUMBER, "changed_paths": list(CHANGED_PATHS)}


def _reviewer_verdict(head: str) -> dict[str, Any]:
    return {"verdict": "APPROVE", "reviewed_head_sha": head, "blockers": [], "warnings": []}


def _live_mergeability(head: str) -> dict[str, Any]:
    return {"head_sha": head, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"}


class Workspace:
    """A temp directory holding the caller-side files a root would pass."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.loop_state = tmp_path / "loop_state.json"
        self.invoke_count = 0

    def write(self, name: str, value: Any) -> str:
        path = self.dir / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return str(path)

    def state(self) -> dict[str, Any]:
        return json.loads(self.loop_state.read_text(encoding="utf-8"))

    def write_state(self, value: Any) -> None:
        self.loop_state.write_text(json.dumps(value), encoding="utf-8")

    # -- production CLI wrappers ------------------------------------------

    def _run(self, argv: list[str]) -> tuple[int, dict[str, Any]]:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *argv],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.stdout.strip(), f"no stdout: rc={completed.returncode} stderr={completed.stderr}"
        return completed.returncode, json.loads(completed.stdout.strip().splitlines()[-1])

    def _binding_args(self, head: str, body: str, hashes: list[str], tag: str) -> list[str]:
        return [
            "--loop-state-file",
            str(self.loop_state),
            "--expected-head-sha",
            head,
            "--expected-contract-body-sha256",
            body,
            "--expected-command-hashes-file",
            self.write(f"hashes_{tag}.json", hashes),
        ]

    def adjudicate(
        self,
        *,
        head: str = HEAD_A,
        body: str = BODY_A,
        runtime_only: bool = False,
        verdict: Any = None,
        verdict_path: str | None = None,
        hashes: list[str] | None = None,
        issue_number: int | None = ISSUE_NUMBER,
        pr_number: int | None = PR_NUMBER,
        tag: str = "adj",
    ) -> tuple[int, dict[str, Any]]:
        """Run `step4-adjudicate` (the only reviewer-dispatch entrance)."""
        hashes = hashes if hashes is not None else _hashes(runtime_only)
        verdict = verdict if verdict is not None else _test_verdict(head, body, runtime_only)
        argv = [
            "step4-adjudicate",
            *self._binding_args(head, body, hashes, tag),
            "--test-verdict-file",
            verdict_path if verdict_path is not None else self.write(f"verdict_{tag}.json", verdict),
            "--contract-snapshot-file",
            self.write(f"snapshot_{tag}.json", _contract_snapshot(body, runtime_only)),
            "--diff-summary-file",
            self.write(f"diff_{tag}.json", _diff_summary(head)),
            "--allowed-paths-file",
            self.write(f"allowed_{tag}.json", ALLOWED_PATHS),
        ]
        if issue_number is not None:
            argv += ["--expected-issue-number", str(issue_number)]
        if pr_number is not None:
            argv += ["--expected-pr-number", str(pr_number)]
        rc, payload = self._run(argv)
        if rc == 0:
            self.invoke_count += 1
        return rc, payload

    def reuse_stored(
        self,
        *,
        head: str = HEAD_A,
        body: str = BODY_A,
        runtime_only: bool = False,
        hashes: list[str] | None = None,
        tag: str = "reuse",
    ) -> tuple[int, dict[str, Any]]:
        hashes = hashes if hashes is not None else _hashes(runtime_only)
        rc, payload = self._run(["step4-adjudicate", "--reuse-stored", *self._binding_args(head, body, hashes, tag)])
        if rc == 0:
            self.invoke_count += 1
        return rc, payload

    def step4_gate(
        self,
        *,
        head: str = HEAD_A,
        body: str = BODY_A,
        runtime_only: bool = False,
        hashes: list[str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        hashes = hashes if hashes is not None else _hashes(runtime_only)
        return self._run(["step4-gate", *self._binding_args(head, body, hashes, "gate")])

    def terminal_gate(
        self,
        *,
        dispatch_seq: int,
        head: str = HEAD_A,
        body: str = BODY_A,
        runtime_only: bool = False,
        hashes: list[str] | None = None,
        reviewer_head: str | None = None,
        verdict: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        hashes = hashes if hashes is not None else _hashes(runtime_only)
        argv = [
            "step5-terminal-gate",
            *self._binding_args(head, body, hashes, "term"),
            "--reviewer-verdict-file",
            self.write("reviewer_verdict.json", verdict or _reviewer_verdict(reviewer_head or head)),
            "--live-mergeability-file",
            self.write("live_mergeability.json", _live_mergeability(head)),
            "--dispatch-seq",
            str(dispatch_seq),
        ]
        return self._run(argv)


def _key(head: str = HEAD_A, body: str = BODY_A, runtime_only: bool = False, hashes: list[str] | None = None) -> str:
    return mod.step4_binding_key(
        head_sha=head,
        contract_body_sha256=body,
        command_hashes=hashes if hashes is not None else _hashes(runtime_only),
    )


def _dispatched(tmp_path: Path, *, runtime_only: bool = False) -> Workspace:
    """A workspace where the canonical PASS was persisted and dispatch seq=1
    was recorded through the production `step4-adjudicate` entrance."""
    ws = Workspace(tmp_path)
    rc, payload = ws.adjudicate(runtime_only=runtime_only)
    assert rc == 0, payload
    assert payload["seq"] == 1
    return ws


# --- AC1 / AC3 -------------------------------------------------------------


def test_ordinary_vc_canonical_pass_opens_dispatch_once(tmp_path):
    ws = Workspace(tmp_path)

    rc, payload = ws.adjudicate()

    assert rc == 0
    assert payload["invoke_pr_reviewer"] is True
    assert payload["binding_key"] == _key()
    assert payload["seq"] == 1
    assert payload["adjudication"]["blocking"] is False
    assert ws.invoke_count == 1
    assert ws.state()["dispatch"] == {"binding_key": _key(), "seq": 1}
    assert ws.step4_gate()[0] == 0


def test_runtime_only_canonical_pass_opens_dispatch_once(tmp_path):
    ws = Workspace(tmp_path)

    rc, payload = ws.adjudicate(runtime_only=True)

    assert rc == 0
    assert payload["seq"] == 1
    assert payload["binding_key"] == _key(runtime_only=True)
    assert ws.invoke_count == 1
    stored = ws.state()["vc_adjudication"][_key(runtime_only=True)]
    runtime_entry = [entry for entry in stored["per_ac"] if entry["ac"] == "AC3"]
    assert runtime_entry and runtime_entry[0]["reason_code"] == "runtime_only_current_head_binding_pass"
    assert stored["blocking"] is False


# --- AC2 --------------------------------------------------------------------


def test_required_vc_fail_skip_missing_malformed_blocks_dispatch_and_approval(tmp_path):
    cases = {
        "fail": dict(
            runtime_only=False,
            verdict=_test_verdict(
                HEAD_A, BODY_A, False, ac_overrides={"AC1": {"exit_code": 1, "status": "fail"}}, result="FAIL"
            ),
        ),
        "skip": dict(
            runtime_only=False,
            verdict=_test_verdict(
                HEAD_A, BODY_A, False, ac_overrides={"AC2": {"exit_code": 77, "status": "skip"}}, result="PARTIAL"
            ),
        ),
        "runtime_only_current_skip": dict(
            runtime_only=True,
            verdict=_test_verdict(
                HEAD_A, BODY_A, True, ac_overrides={"AC3": {"exit_code": 77, "status": "skip"}}, result="PARTIAL"
            ),
        ),
        "missing": dict(runtime_only=False, verdict=_test_verdict(HEAD_A, BODY_A, False, drop_acs=("AC2",))),
        "malformed": dict(runtime_only=False, verdict="{this is not json"),
    }
    for name, case in cases.items():
        case_dir = tmp_path / name
        case_dir.mkdir()
        runtime_only = case["runtime_only"]

        # (a) no PASS has ever been stored: reviewer is not dispatchable and an
        # existing reviewer APPROVE cannot become terminal `approved`.
        fresh = Workspace(case_dir / "fresh")
        fresh.dir.mkdir()
        verdict = case["verdict"]
        verdict_path = fresh.write("bad_verdict.json", verdict) if isinstance(verdict, str) else None
        rc, payload = fresh.adjudicate(
            runtime_only=runtime_only,
            verdict=None if isinstance(verdict, str) else verdict,
            verdict_path=verdict_path,
        )
        assert rc == 1, (name, payload)
        assert fresh.invoke_count == 0, name
        assert "dispatch" not in fresh.state(), name
        term_rc, term = fresh.terminal_gate(dispatch_seq=1, runtime_only=runtime_only)
        assert term_rc == 1 and term["route"] != "approved", (name, term)

        # (b) a PASS exists for the same binding and the canonical
        # re-verification is blocking: the old PASS must not open the gate
        # and the old reviewer APPROVE (seq=1) must not become `approved`.
        stale_ws = Workspace(case_dir / "stale")
        stale_ws.dir.mkdir()
        assert stale_ws.adjudicate(runtime_only=runtime_only)[0] == 0
        verdict_path = stale_ws.write("bad_verdict.json", verdict) if isinstance(verdict, str) else None
        rc, payload = stale_ws.adjudicate(
            runtime_only=runtime_only,
            verdict=None if isinstance(verdict, str) else verdict,
            verdict_path=verdict_path,
            tag="again",
        )
        assert rc == 1, (name, payload)
        assert stale_ws.invoke_count == 1, name
        assert stale_ws.step4_gate(runtime_only=runtime_only)[0] == 1, name
        term_rc, term = stale_ws.terminal_gate(dispatch_seq=1, runtime_only=runtime_only)
        assert term_rc == 1, (name, term)
        assert term["route"] == "continue_loop"
        assert term["reason_code"] == "vc_gate_blocking"
        assert term["rerun_required"]["verification"] is True


# --- AC5 --------------------------------------------------------------------


def test_same_binding_pass_then_blocking_reverification_invalidates_pass(tmp_path):
    ws = _dispatched(tmp_path)
    assert ws.step4_gate()[0] == 0

    failing = _test_verdict(
        HEAD_A, BODY_A, False, ac_overrides={"AC1": {"exit_code": 1, "status": "fail"}}, result="FAIL"
    )
    rc, payload = ws.adjudicate(verdict=failing, tag="reverify")

    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert _key() not in ws.state().get("vc_adjudication", {})
    assert ws.step4_gate()[0] == 1
    assert ws.reuse_stored()[0] == 1
    assert ws.invoke_count == 1


def test_valid_binding_without_reverification_reuses_pass(tmp_path):
    ws = _dispatched(tmp_path)
    before_vc = copy.deepcopy(ws.state()["vc_adjudication"])

    rc, payload = ws.reuse_stored()

    assert rc == 0
    assert payload["invoke_pr_reviewer"] is True
    assert payload["reused"] is True
    assert payload["seq"] == 2
    assert ws.invoke_count == 2
    assert ws.state()["vc_adjudication"] == before_vc
    assert ws.state()["dispatch"] == {"binding_key": _key(), "seq": 2}


# --- AC6 --------------------------------------------------------------------


def test_stale_binding_blocks_terminal_approval(tmp_path):
    ws = _dispatched(tmp_path)

    # Live binding moved to a new head (reviewer result is for that new head).
    rc, payload = ws.step4_gate(head=HEAD_B)
    assert rc == 1 and payload["invoke_pr_reviewer"] is False
    term_rc, term = ws.terminal_gate(dispatch_seq=1, head=HEAD_B)
    assert term_rc == 1 and term["route"] == "continue_loop"
    assert term["reason_code"] == "binding_changed_since_dispatch"

    # Stale loop_state: the dispatch is still there but the stored PASS is gone.
    state = ws.state()
    state["vc_adjudication"] = {}
    ws.write_state(state)
    term_rc, term = ws.terminal_gate(dispatch_seq=1)
    assert term_rc == 1 and term["reason_code"] == "vc_gate_blocking"
    assert term["route"] != "approved"


def test_resume_and_existing_reviewer_result_reuse_use_same_gate(tmp_path, monkeypatch):
    ws = _dispatched(tmp_path)
    saved_seq_file = ws.dir / "reviewer_seq.txt"
    saved_seq_file.write_text("1", encoding="utf-8")

    # Resume / existing reviewer result reuse: no `step4-adjudicate`, the saved
    # seq goes straight to `step5-terminal-gate`; the dispatch count stays 1.
    saved_seq = int(saved_seq_file.read_text(encoding="utf-8"))
    rc, term = ws.terminal_gate(dispatch_seq=saved_seq)
    assert rc == 0 and term["route"] == "approved"
    assert ws.invoke_count == 1

    # In-process: both dispatch (`--reuse-stored`) and terminal approval call
    # the one gate function; replacing evaluate_step4_vc_gate closes both.
    calls = {"count": 0}
    real_gate = mod.step4_gate_from_loop_state

    def _spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(mod, "step4_gate_from_loop_state", _spy)
    state = ws.state()
    binding = dict(
        expected_head_sha=HEAD_A,
        expected_contract_body_sha256=BODY_A,
        expected_command_hashes=_hashes(False),
    )
    exit_code, _ = mod.step4_adjudicate(copy.deepcopy(state), reuse_stored=True, **binding)
    assert exit_code == 0
    exit_code, _ = mod.step5_terminal_gate(
        copy.deepcopy(state),
        _reviewer_verdict(HEAD_A),
        _live_mergeability(HEAD_A),
        dispatch_seq=1,
        **binding,
    )
    assert exit_code == 0
    assert calls["count"] == 2

    monkeypatch.setattr(
        mod,
        "evaluate_step4_vc_gate",
        lambda *a, **k: {"invoke_pr_reviewer": False, "reason_code": "forced_blocking"},
    )
    exit_code, _ = mod.step4_adjudicate(copy.deepcopy(state), reuse_stored=True, **binding)
    assert exit_code == 1
    exit_code, term = mod.step5_terminal_gate(
        copy.deepcopy(state),
        _reviewer_verdict(HEAD_A),
        _live_mergeability(HEAD_A),
        dispatch_seq=1,
        **binding,
    )
    assert exit_code == 1 and term["reason_code"] == "vc_gate_blocking"
    assert calls["count"] == 4


def test_gate_forced_blocking_stops_dispatch_and_terminal_approval(tmp_path, monkeypatch):
    # CLI: stale loop_state (a PASS stored only for another binding).
    ws = Workspace(tmp_path / "stale")
    ws.dir.mkdir()
    assert ws.adjudicate(head=HEAD_B)[0] == 0
    ws.invoke_count = 0
    state = ws.state()
    state.pop("dispatch")
    ws.write_state(state)
    assert ws.reuse_stored(head=HEAD_A)[0] == 1
    assert ws.invoke_count == 0
    rc, term = ws.terminal_gate(dispatch_seq=1, head=HEAD_A)
    assert rc == 1 and term["route"] != "approved"

    # CLI: corrupt loop_state is malformed (exit 2) and approves nothing.
    corrupt = Workspace(tmp_path / "corrupt")
    corrupt.dir.mkdir()
    corrupt.loop_state.write_text("{not json", encoding="utf-8")
    rc, _ = corrupt.reuse_stored()
    assert rc == 2
    rc, _ = corrupt.adjudicate()
    assert rc == 2
    assert corrupt.loop_state.read_text(encoding="utf-8") == "{not json"
    rc, _ = corrupt.terminal_gate(dispatch_seq=1)
    assert rc == 2
    assert corrupt.invoke_count == 0

    # In-process: forcing the gate blocking keeps the dispatch count at 0 even
    # for a fully valid canonical PASS input, and terminal approval is closed.
    monkeypatch.setattr(
        mod,
        "evaluate_step4_vc_gate",
        lambda *a, **k: {"invoke_pr_reviewer": False, "reason_code": "forced_blocking"},
    )
    loop_state: dict[str, Any] = {}
    exit_code, payload = mod.step4_adjudicate(
        loop_state,
        expected_head_sha=HEAD_A,
        expected_contract_body_sha256=BODY_A,
        expected_command_hashes=_hashes(False),
        test_verdict=_test_verdict(HEAD_A, BODY_A, False),
        contract_snapshot=_contract_snapshot(BODY_A, False),
        diff_summary=_diff_summary(HEAD_A),
        allowed_paths=ALLOWED_PATHS,
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )
    assert exit_code == 1 and payload["invoke_pr_reviewer"] is False
    assert "dispatch" not in loop_state
    loop_state["dispatch"] = {"binding_key": _key(), "seq": 1}
    exit_code, term = mod.step5_terminal_gate(
        loop_state,
        _reviewer_verdict(HEAD_A),
        _live_mergeability(HEAD_A),
        expected_head_sha=HEAD_A,
        expected_contract_body_sha256=BODY_A,
        expected_command_hashes=_hashes(False),
        dispatch_seq=1,
    )
    assert exit_code == 1 and term["route"] != "approved"


# --- AC4 --------------------------------------------------------------------


def test_adapt_failure_output_is_not_consumable(tmp_path):
    ws = Workspace(tmp_path)
    broken = _test_verdict(HEAD_A, BODY_A, False)
    del broken["runtime_ac_results"][0]["command_hash"]
    before_files = {path.name for path in tmp_path.iterdir()}

    rc, payload = ws.adjudicate(verdict=broken)

    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert "adapt_failed" in payload["adjudication"]["errors"]
    # No intermediate output exists at all and nothing was dispatched.
    created = {path.name for path in tmp_path.iterdir()} - before_files
    assert not [name for name in created if "current_vc" in name or "adapt" in name or name.endswith(".tmp")]
    assert ws.state().get("vc_adjudication", {}) == {}
    assert "dispatch" not in ws.state()
    assert ws.step4_gate()[0] == 1


def test_adapt_error_invalidates_existing_pass_for_caller_binding(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.adjudicate(head=HEAD_A)[0] == 0
    assert ws.adjudicate(head=HEAD_B, tag="other")[0] == 0
    assert set(ws.state()["vc_adjudication"]) == {_key(head=HEAD_A), _key(head=HEAD_B)}

    unusable = {"schema": "NOT_A_TEST_VERDICT", "result": "PASS"}
    rc, payload = ws.adjudicate(head=HEAD_A, verdict=unusable, tag="bad")

    assert rc == 1
    assert set(ws.state()["vc_adjudication"]) == {_key(head=HEAD_B)}
    assert ws.step4_gate(head=HEAD_A)[0] == 1
    assert ws.step4_gate(head=HEAD_B)[0] == 0

    # adapt errors with a convertible-but-incomplete report take the same path.
    incomplete = _test_verdict(HEAD_B, BODY_A, False)
    del incomplete["head_sha"]
    rc, _ = ws.adjudicate(head=HEAD_B, verdict=incomplete, tag="bad2")
    assert rc == 1
    assert ws.state()["vc_adjudication"] == {}


def test_missing_final_result_closes_gate(tmp_path):
    ws = _dispatched(tmp_path)

    rc, payload = ws.adjudicate(verdict_path=str(tmp_path / "does_not_exist.json"), tag="nofinal")

    assert rc == 1
    assert payload["invoke_pr_reviewer"] is False
    assert payload["reason_code"] == "adjudication_missing_or_malformed"
    assert ws.step4_gate()[0] == 1
    assert ws.state()["dispatch"]["seq"] == 1
    assert ws.invoke_count == 1


# --- AC1 producer contract / AC9 ------------------------------------------


def test_test_runner_documented_report_yields_certified_pass(tmp_path):
    doc = STEP2_DOC.read_text(encoding="utf-8")
    section = doc.split("### 独立した実行事実", 1)[1].split("### GitHub 由来情報", 1)[0]
    documented = set(re.findall(r"^\| `([a-z0-9_]+)(?:\[\])?`", section, flags=re.MULTILINE))
    for pair in re.findall(r"^\| `([a-z0-9_]+)` / `([a-z0-9_]+)`", section, flags=re.MULTILINE):
        documented.update(pair)
    for triple in re.findall(r"^\| `([a-z0-9_]+)` / `([a-z0-9_]+)` / `([a-z0-9_]+)`", section, flags=re.MULTILINE):
        documented.update(triple)
    assert "generated_at" in documented and "command_hash" in section
    # The documented set is exactly what the producer report below carries
    # (top level), and it contains no GitHub-derived field.
    report = _test_verdict(HEAD_A, BODY_A, False)
    assert set(report) == documented
    for github_only in ("producer_kind", "workflow_run_id", "check_run_id", "artifact", "producer_receipt"):
        assert github_only not in report
    assert "UTC" in section and "RFC 3339" in section

    for runtime_only in (False, True):
        ws = Workspace(tmp_path / f"runtime_{runtime_only}")
        ws.dir.mkdir()
        rc, payload = ws.adjudicate(runtime_only=runtime_only)
        assert rc == 0, payload
        assert payload["adjudication"]["blocking"] is False

    # A report without generated_at never certifies (consumer stays strict).
    ws = Workspace(tmp_path / "no_generated_at")
    ws.dir.mkdir()
    rc, payload = ws.adjudicate(verdict=_test_verdict(HEAD_A, BODY_A, False, generated_at=None))
    assert rc == 1 and payload["adjudication"]["blocking"] is True


def test_nonblocking_classifications_are_preserved():
    key_a = {"kind": "pytest_nodeid", "key": "tests/unrelated/test_x.py::test_a"}
    key_b = {"kind": "pytest_nodeid", "key": "tests/unrelated/test_x.py::test_b"}

    def _snapshot(failure_keys: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "schema": "baseline_vc_preflight/v1",
            "status": "go",
            "body_sha256": BODY_A,
            "results": [
                {
                    "ac": "AC1",
                    "command_hash": H_AC1,
                    "classification": "expected_fail",
                    "exit_code": 1,
                    "failure_keys": failure_keys,
                }
            ],
        }

    def _current(failure_keys: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "schema": "baseline_vc_preflight/v1",
            "generated_at": GENERATED_AT,
            "status": "fail",
            "errors": [],
            "fallback_detected": False,
            "human_review_required": False,
            "stop_condition_triggered": False,
            "source": {"body_sha256": BODY_A},
            "head_sha": HEAD_A,
            "reviewed_head_sha": HEAD_A,
            "results": [{"ac": "AC1", "command_hash": H_AC1, "exit_code": 1, "failure_keys": failure_keys}],
        }

    # out_of_scope_fail: an unrelated failure that is a subset of the baseline
    # failure index but not an exact baseline signature, with a diff present.
    out_of_scope = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot([key_a, key_b]),
        current_vc_result=_current([key_a]),
        diff_summary=_diff_summary(HEAD_A),
        allowed_paths=ALLOWED_PATHS,
    )
    assert out_of_scope["per_ac"][0]["status"] == "out_of_scope_fail"
    assert out_of_scope["blocking"] is False

    # pre_existing_fail: exact baseline signature with no diff.
    pre_existing = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot([key_a]),
        current_vc_result=_current([key_a]),
        diff_summary={"head_sha": HEAD_A, "pr_number": PR_NUMBER, "changed_paths": []},
        allowed_paths=ALLOWED_PATHS,
    )
    assert pre_existing["per_ac"][0]["status"] == "pre_existing_fail"
    assert pre_existing["blocking"] is False

    for result in (out_of_scope, pre_existing):
        loop_state: dict[str, Any] = {}
        mod.step4_persist_vc_adjudication(
            loop_state,
            head_sha=HEAD_A,
            contract_body_sha256=BODY_A,
            command_hashes=[H_AC1],
            adjudication_result=result,
        )
        decision = mod.step4_gate_from_loop_state(
            loop_state,
            expected_head_sha=HEAD_A,
            expected_contract_body_sha256=BODY_A,
            expected_command_hashes=[H_AC1],
        )
        assert decision["invoke_pr_reviewer"] is True


# --- AC3 --------------------------------------------------------------------


def test_runtime_only_requires_independently_obtained_issue_and_pr_numbers(tmp_path):
    cases = [
        ("no_issue", dict(issue_number=None), "runtime_only_expected_issue_number_missing"),
        ("no_pr", dict(pr_number=None), "runtime_only_expected_pr_number_missing"),
        ("wrong_issue", dict(issue_number=ISSUE_NUMBER + 1), "runtime_only_issue_number_mismatch"),
        ("wrong_pr", dict(pr_number=PR_NUMBER + 1), "runtime_only_pr_number_mismatch"),
    ]
    for name, kwargs, error in cases:
        ws = Workspace(tmp_path / name)
        ws.dir.mkdir()
        rc, payload = ws.adjudicate(runtime_only=True, **kwargs)
        assert rc == 1, (name, payload)
        assert error in payload["adjudication"]["errors"], (name, payload)
        assert ws.invoke_count == 0
        assert "dispatch" not in ws.state()

    ok = Workspace(tmp_path / "ok")
    ok.dir.mkdir()
    assert ok.adjudicate(runtime_only=True)[0] == 0

    # The runtime_only command must be an ACTUAL executed pass for the same
    # (AC, command_hash): wrong hash / non-zero exit / fallback keep it closed.
    for name, overrides in {
        "bad_exit": {"exit_code": 2, "status": "fail"},
        "fallback": {"fallback_detected": True},
        "human_review": {"human_review_required": True},
        "stop_condition": {"stop_condition_triggered": True},
    }.items():
        ws = Workspace(tmp_path / name)
        ws.dir.mkdir()
        rc, payload = ws.adjudicate(
            runtime_only=True,
            verdict=_test_verdict(HEAD_A, BODY_A, True, ac_overrides={"AC3": overrides}),
        )
        assert rc == 1, (name, payload)
        assert ws.invoke_count == 0


# --- AC7 / AC8 ---------------------------------------------------------------


def test_reinvoke_after_body_change_rejects_old_reviewer_result(tmp_path):
    ws = _dispatched(tmp_path)
    assert ws.state()["dispatch"] == {"binding_key": _key(), "seq": 1}

    rc, payload = ws.adjudicate(body=BODY_B, tag="body_b")
    assert rc == 0
    assert payload["seq"] == 2
    assert ws.state()["dispatch"] == {"binding_key": _key(body=BODY_B), "seq": 2}

    # The old reviewer result carries seq=1 and is rejected.
    rc, term = ws.terminal_gate(dispatch_seq=1, body=BODY_B)
    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["reason_code"] == "dispatch_seq_mismatch"
    assert term["rerun_required"]["pr_review"] is True


def test_redispatch_with_new_seq_allows_approval(tmp_path):
    ws = _dispatched(tmp_path)
    rc, payload = ws.adjudicate(body=BODY_B, tag="body_b")
    assert rc == 0 and payload["seq"] == 2

    rc, term = ws.terminal_gate(dispatch_seq=2, body=BODY_B)

    assert rc == 0
    assert term["route"] == "approved"
    assert set(term) == {"route", "fail_closed", "reason_code", "selected_action", "rerun_required", "errors"}


def test_step4_gate_remains_read_only(tmp_path):
    ws = _dispatched(tmp_path)
    before = ws.loop_state.read_bytes()

    rc, _ = ws.step4_gate()
    assert rc == 0
    assert ws.loop_state.read_bytes() == before
    rc, _ = ws.step4_gate(head=HEAD_B)
    assert rc == 1
    assert ws.loop_state.read_bytes() == before
    assert ws.state()["dispatch"]["seq"] == 1

    # A state with a stored PASS but no dispatch never gains one from step4-gate.
    state = ws.state()
    state.pop("dispatch")
    ws.write_state(state)
    before = ws.loop_state.read_bytes()
    assert ws.step4_gate()[0] == 0
    assert ws.loop_state.read_bytes() == before
    assert "dispatch" not in ws.state()

    source = inspect.getsource(mod._run_step4_gate)
    assert "_write_json_atomically" not in source and "write_text" not in source


# --- AC11 --------------------------------------------------------------------


def test_persisted_loop_state_gains_only_dispatch_key(tmp_path):
    ws = Workspace(tmp_path)
    assert not ws.loop_state.exists()

    rc, _ = ws.adjudicate()
    assert rc == 0
    assert ws.reuse_stored()[0] == 0

    state = ws.state()
    assert set(state) == {"vc_adjudication", "dispatch"}
    assert set(state["dispatch"]) == {"binding_key", "seq"}
    assert state["dispatch"] == {"binding_key": _key(), "seq": 2}
    assert set(state["vc_adjudication"]) == {_key()}
    # No new route constant: only the three reason_code strings are new.
    route_module = mod._load_route_module()
    assert {name for name in dir(route_module) if name.startswith("ROUTE_")} == {
        "ROUTE_APPROVED",
        "ROUTE_CONTINUE_LOOP",
        "ROUTE_ALREADY_SATISFIED",
        "ROUTE_TO_UPDATE_BRANCH",
        "ROUTE_SCOPE_CLEAN_RECONCILIATION",
        "ROUTE_STALE_HEAD_REREVIEW",
        "ROUTE_HUMAN_ESCALATION",
        "ROUTE_CONFLICT_HARD_STOP",
        "ROUTE_FAIL_CLOSED",
    }
    assert (
        mod.REASON_VC_GATE_BLOCKING,
        mod.REASON_DISPATCH_SEQ_MISMATCH,
        mod.REASON_BINDING_CHANGED_SINCE_DISPATCH,
    ) == ("vc_gate_blocking", "dispatch_seq_mismatch", "binding_changed_since_dispatch")


def test_head_or_command_hash_change_after_dispatch_rejects_old_reviewer_result(tmp_path):
    ws = _dispatched(tmp_path)

    # HEAD changed after dispatch (the reviewer verdict is for the new head so
    # the router approves and the dispatch binding check is what must reject).
    rc, term = ws.terminal_gate(dispatch_seq=1, head=HEAD_B)
    assert rc == 1 and term["reason_code"] == "binding_changed_since_dispatch"
    assert term["rerun_required"] == {"verification": True, "pr_review": True}

    # An old-head reviewer result is already stale at the router.
    rc, term = ws.terminal_gate(dispatch_seq=1, head=HEAD_B, reviewer_head=HEAD_A)
    assert rc == 1 and term["route"] == "route_stale_head_rereview"

    # Command hashes changed after dispatch (different literal commands).
    changed = _hashes(False, swap_first=True)
    rc, term = ws.terminal_gate(dispatch_seq=1, hashes=changed)
    assert rc == 1 and term["reason_code"] == "binding_changed_since_dispatch"

    # Same command hashes in a different order are a different binding too.
    reordered = list(reversed(_hashes(False)))
    rc, term = ws.terminal_gate(dispatch_seq=1, hashes=reordered)
    assert rc == 1 and term["reason_code"] == "binding_changed_since_dispatch"

    # Unchanged binding still approves (control).
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 0 and term["route"] == "approved"


def test_stage_a_b_reviewer_recovery_uses_same_gate(tmp_path):
    ws = _dispatched(tmp_path)
    before = ws.loop_state.read_bytes()

    # Stage A / B recovery of an ALREADY dispatched reviewer does not call
    # step4-adjudicate: the dispatch count stays 1, the original seq (saved
    # when the reviewer was dispatched) goes to step5-terminal-gate, and the
    # gate evaluation (and loop_state) stay untouched.
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 0 and term["route"] == "approved"
    assert ws.invoke_count == 1
    assert ws.loop_state.read_bytes() == before

    # The same gate closes recovery when the VC evidence went stale.
    state = ws.state()
    state["vc_adjudication"] = {}
    ws.write_state(state)
    rc, term = ws.terminal_gate(dispatch_seq=1)
    assert rc == 1 and term["reason_code"] == "vc_gate_blocking"
    assert ws.invoke_count == 1

    step4_doc = STEP4_DOC.read_text(encoding="utf-8")
    assert "Stage A" in step4_doc and "step5-terminal-gate" in step4_doc
    assert "同等の永続化" not in step4_doc


def test_reuse_stored_pass_records_dispatch_without_test_runner(tmp_path):
    ws = _dispatched(tmp_path)

    # Only --loop-state-file and the caller-supplied expected arguments.
    rc, payload = ws.reuse_stored(tag="only_expected")
    assert rc == 0 and payload["seq"] == 2 and payload["invoke_pr_reviewer"] is True
    assert ws.state()["dispatch"]["seq"] == 2
    assert not any(path.name.startswith("verdict_reuse") for path in tmp_path.iterdir())

    # No stored PASS -> exit 1 and no state is created.
    empty = Workspace(tmp_path / "empty")
    empty.dir.mkdir()
    rc, payload = empty.reuse_stored()
    assert rc == 1 and payload["invoke_pr_reviewer"] is False and payload["seq"] is None
    assert not empty.loop_state.exists()

    # Only the caller-supplied expected arguments are required by the CLI.
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "step4-adjudicate", "--reuse-stored", "--loop-state-file", "x"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2


def test_terminal_gate_resolves_semantic_ambiguity(tmp_path, monkeypatch):
    sha = "0" * 40

    def _git(repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    branch = _git(repo, "symbolic-ref", "--short", "HEAD")
    _git(repo, "checkout", "-q", "-b", "candidate")
    (repo / "f.txt").write_text("candidate edit\n")
    _git(repo, "commit", "-q", "-am", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", branch)
    (repo / "f.txt").write_text("main edit\n")
    _git(repo, "commit", "-q", "-am", "main")
    current_sha = _git(repo, "rev-parse", "HEAD")
    assert sha not in (candidate_sha, current_sha)

    live = _live_mergeability(HEAD_A)
    live["main_drift"] = {
        "current_base_sha": current_sha,
        "evidence_base_sha": candidate_sha,
        "allowed_paths_snapshot_base_sha": current_sha,
        "allowed_paths": ["f.txt"],
        "latest_main_net_diff": ["f.txt"],
        "expected_old_sha": current_sha,
        "observed_old_sha": current_sha,
        # `semantic_ambiguity` is intentionally omitted: it must be resolved
        # from the real git oracle, never guessed or fixed by the caller.
    }
    state: dict[str, Any] = {}
    exit_code, _ = mod.step4_adjudicate(
        state,
        expected_head_sha=HEAD_A,
        expected_contract_body_sha256=BODY_A,
        expected_command_hashes=_hashes(False),
        test_verdict=_test_verdict(HEAD_A, BODY_A, False),
        contract_snapshot=_contract_snapshot(BODY_A, False),
        diff_summary=_diff_summary(HEAD_A),
        allowed_paths=ALLOWED_PATHS,
        expected_issue_number=ISSUE_NUMBER,
        expected_pr_number=PR_NUMBER,
    )
    assert exit_code == 0
    kwargs = dict(
        expected_head_sha=HEAD_A,
        expected_contract_body_sha256=BODY_A,
        expected_command_hashes=_hashes(False),
        dispatch_seq=1,
    )

    exit_code, term = mod.step5_terminal_gate(state, _reviewer_verdict(HEAD_A), live, cwd=repo, **kwargs)
    assert exit_code == 1
    assert term["route"] == "fail_closed" and term["reason_code"] == "semantic_ambiguity"

    # Without an explicit cwd the production repository root is the oracle cwd
    # (the CLI path), through the existing public wrapper only.
    route_module = mod._load_route_module()
    seen: dict[str, Any] = {}
    real_wrapper = route_module.route_loop_verdict_v2_resolve_semantic_ambiguity

    def _spy(reviewer_verdict: Any, live_mergeability: Any, *, cwd: Any) -> Any:
        seen["cwd"] = cwd
        return real_wrapper(reviewer_verdict, live_mergeability, cwd=cwd)

    monkeypatch.setattr(route_module, "route_loop_verdict_v2_resolve_semantic_ambiguity", _spy)
    exit_code, term = mod.step5_terminal_gate(state, _reviewer_verdict(HEAD_A), _live_mergeability(HEAD_A), **kwargs)
    assert exit_code == 0 and term["route"] == "approved"
    assert Path(seen["cwd"]) == mod._REPO_ROOT


def test_dispatch_seq_mismatch_reason_code(tmp_path):
    ws = _dispatched(tmp_path)
    assert ws.reuse_stored()[0] == 0  # seq -> 2

    rc, term = ws.terminal_gate(dispatch_seq=1)

    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["reason_code"] == "dispatch_seq_mismatch"
    assert term["rerun_required"] == {"verification": False, "pr_review": True}

    # Evaluation order: seq mismatch wins over binding change and VC blocking.
    state = ws.state()
    state["vc_adjudication"] = {}
    ws.write_state(state)
    rc, term = ws.terminal_gate(dispatch_seq=1, body=BODY_B)
    assert term["reason_code"] == "dispatch_seq_mismatch"

    # Non-approved routes are passed through unchanged.
    rc, term = ws.terminal_gate(
        dispatch_seq=2,
        verdict={"verdict": "REQUEST_CHANGES", "reviewed_head_sha": HEAD_A, "blockers": ["x"], "warnings": []},
    )
    assert rc == 1 and term["route"] == "continue_loop" and term["reason_code"] is None


def test_missing_test_verdict_invalidates_and_returns_rerun(tmp_path):
    ws = _dispatched(tmp_path)
    other = Workspace(tmp_path)  # same loop_state file, other binding
    assert other.adjudicate(head=HEAD_B, tag="other")[0] == 0
    assert set(ws.state()["vc_adjudication"]) == {_key(), _key(head=HEAD_B)}

    rc, payload = ws.adjudicate(verdict_path=str(tmp_path / "missing_report.json"), tag="missing")

    assert rc == 1  # rerun, NOT malformed (2)
    assert payload["invoke_pr_reviewer"] is False
    assert set(ws.state()["vc_adjudication"]) == {_key(head=HEAD_B)}
    assert ws.step4_gate()[0] == 1


def test_body_change_after_dispatch_reports_binding_changed(tmp_path):
    ws = _dispatched(tmp_path)

    rc, term = ws.terminal_gate(dispatch_seq=1, body=BODY_B)

    assert rc == 1
    assert term["route"] == "continue_loop"
    assert term["reason_code"] == "binding_changed_since_dispatch"
    assert term["rerun_required"]["verification"] is True
    assert term["rerun_required"]["pr_review"] is True


def test_multiple_stored_bindings_evaluate_only_caller_binding(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.adjudicate(head=HEAD_A)[0] == 0
    assert ws.adjudicate(head=HEAD_B, tag="b")[0] == 0
    assert set(ws.state()["vc_adjudication"]) == {_key(head=HEAD_A), _key(head=HEAD_B)}

    # Tamper only binding A's stored entry so it is no longer a valid PASS.
    state = ws.state()
    state["vc_adjudication"][_key(head=HEAD_A)]["blocking"] = True
    ws.write_state(state)

    assert ws.step4_gate(head=HEAD_A)[0] == 1
    assert ws.step4_gate(head=HEAD_B)[0] == 0
    assert ws.reuse_stored(head=HEAD_A)[0] == 1
    rc, payload = ws.reuse_stored(head=HEAD_B)
    assert rc == 0 and payload["binding_key"] == _key(head=HEAD_B)
    # A binding that was never stored is evaluated on its own and stays closed.
    assert ws.step4_gate(head="c" * 40)[0] == 1


def test_absent_or_malformed_dispatch_returns_dispatch_seq_mismatch(tmp_path):
    ws = _dispatched(tmp_path)
    good_state = ws.state()
    bad_dispatches: list[Any] = [
        "absent",
        "not-a-mapping",
        {"binding_key": _key()},
        {"seq": 1},
        {"binding_key": 1, "seq": 1},
        {"binding_key": _key(), "seq": "1"},
        {"binding_key": _key(), "seq": True},
        {"binding_key": _key(), "seq": 0},
    ]
    for bad in bad_dispatches:
        state = copy.deepcopy(good_state)
        if bad == "absent":
            state.pop("dispatch")
        else:
            state["dispatch"] = bad
        ws.write_state(state)
        rc, term = ws.terminal_gate(dispatch_seq=1)
        assert rc == 1, bad
        assert term["route"] == "continue_loop", bad
        assert term["reason_code"] == "dispatch_seq_mismatch", bad
        assert term["rerun_required"]["pr_review"] is True, bad

    # step4-adjudicate refuses to extend a corrupt dispatch record (exit 2,
    # nothing written), so a seq can never be silently reset to 1.
    state = copy.deepcopy(good_state)
    state["dispatch"] = {"binding_key": _key(), "seq": "x"}
    ws.write_state(state)
    before = ws.loop_state.read_bytes()
    rc, _ = ws.adjudicate(tag="after_corrupt")
    assert rc == 2
    assert ws.loop_state.read_bytes() == before

    # Unreadable inputs of the terminal gate are malformed (exit 2).
    ws.write_state(good_state)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "step5-terminal-gate",
            *ws._binding_args(HEAD_A, BODY_A, _hashes(False), "term"),
            "--reviewer-verdict-file",
            str(tmp_path / "no_such_verdict.json"),
            "--live-mergeability-file",
            ws.write("live.json", _live_mergeability(HEAD_A)),
            "--dispatch-seq",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2


# --- AC10 static / documentation binding ------------------------------------


def test_documents_bind_the_canonical_entrances():
    step4_doc = STEP4_DOC.read_text(encoding="utf-8")
    step5_doc = STEP5_DOC.read_text(encoding="utf-8")
    for token in ("step4-adjudicate", "--expected-issue-number", "--expected-pr-number", "--reuse-stored"):
        assert token in step4_doc, token
    for token in ("step5-terminal-gate", "--dispatch-seq"):
        assert token in step5_doc, token
    assert "同等の永続化" not in step4_doc

