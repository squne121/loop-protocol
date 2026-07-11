#!/usr/bin/env python3
"""Tests for the overlap preflight hard gate in open_pr.py (Issue #1458).

`open_pr.py` runs a fail-closed gate immediately before `gh pr create` (after
existing-PR detection and dry-run handling). This gate:

1. Reads back the stored `overlap_preflight` evidence file and verifies its
   embedded `evidence_sha256` integrity (E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING /
   E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID).
2. Re-executes `check_implementation_overlap.py` online (subprocess) to fetch
   fresh evidence (E_OVERLAP_PREFLIGHT_SOURCE_FAILURE on timeout / non-zero
   exit / non-JSON output).
3. Compares the fresh `decision_inputs_sha256` against the caller-supplied
   expected value to detect drift (E_OVERLAP_PREFLIGHT_DRIFT).
4. Verifies a safety predicate over route / source / validation_errors /
   dependency_resolution / current_issue (E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE).

The gate is forced (cannot be skipped via omission) when the linked issue
carries the `phase/implementation` label, even if `overlap_preflight` is
unspecified or `required: false` (AC2, bypass-via-omission mitigation).
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import open_pr


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr_body"


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def write_temp_body(body: str) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False)
    handle.write(body)
    handle.flush()
    handle.close()
    return handle.name


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_stored_evidence(
    *,
    decision_inputs_sha256: str = "sha256:" + "a" * 64,
    route: str = "proceed",
    current_issue_number: int = 1458,
    source_complete: bool = True,
    source_saturated: bool = False,
    validation_errors: dict | None = None,
    unresolved_refs: list | None = None,
    blocking_predecessor: object | None = None,
) -> dict:
    """Build a stored evidence dict with a correctly-computed embedded
    evidence_sha256 (mirrors check_implementation_overlap.py's build_evidence
    canonicalization contract)."""
    body = {
        "schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA,
        "current_issue": {"number": current_issue_number, "allowed_paths": []},
        "source": {"complete": source_complete, "saturated": source_saturated, "collected_at": "2026-07-11T00:00:00Z"},
        "candidates": [],
        "dependency_resolution": {
            "blocked_by_refs": [],
            "blocking_predecessor": blocking_predecessor,
            "closed_predecessors": [],
            "unresolved_refs": unresolved_refs if unresolved_refs is not None else [],
        },
        "validation_errors": validation_errors if validation_errors is not None else {},
        "route": route,
        "decision_inputs_sha256": decision_inputs_sha256,
    }
    canonical = _canonical_json(body)
    body["evidence_sha256"] = f"sha256:{_sha256(canonical)}"
    return body


def write_evidence_file(evidence: dict) -> Path:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False)
    handle.write(json.dumps(evidence))
    handle.flush()
    handle.close()
    return Path(handle.name)


def fresh_evidence_from_stored(stored: dict, **overrides: object) -> dict:
    """Build a 'fresh' online re-run result payload from a stored evidence
    dict, applying overrides (used to simulate drift / unsafe conditions)."""
    fresh = json.loads(json.dumps(stored))
    fresh.update(overrides)
    return fresh


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _common_monkeypatches(monkeypatch: pytest.MonkeyPatch, linked_issue: int = 1458) -> None:
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
    monkeypatch.setattr(open_pr, "resolve_branch", lambda: f"worktree-issue-{linked_issue}-test")
    monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
    monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
    monkeypatch.setattr(
        open_pr,
        "_run_pr_body_validator",
        lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
    )
    monkeypatch.setattr(
        open_pr,
        "_run_japanese_content_validator",
        lambda body_text, threshold=0.1: {
            "status": "pass",
            "failed_blocks": 0,
            "aggregate_ratio": 0.5,
            "threshold": 0.1,
            "body_sha256": "",
            "stderr": "",
        },
    )
    monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    linked_issue: int,
    extra_args: list[str],
    create_pr_result: str = "https://github.com/squne121/loop-protocol/pull/9999",
    patch_create_pr: bool = True,
) -> tuple[int, list[str], bool]:
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    output_lines: list[str] = []
    create_called = {"value": False}

    def capture_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        output_lines.append(sep.join(str(a) for a in args))

    def fake_create_pr(*args, **kwargs):
        create_called["value"] = True
        return create_pr_result

    try:
        if patch_create_pr:
            monkeypatch.setattr(open_pr, "create_pr", fake_create_pr)
        monkeypatch.setattr("builtins.print", capture_print)
        base_args = [
            "--pr-title", "feat: test",
            "--linked-issue", str(linked_issue),
            "--publish", "yes",
            "--pr-body-file", body_path,
        ]
        base_args.extend(extra_args)
        rc = open_pr.main(base_args)
        return rc, output_lines, create_called["value"]
    finally:
        Path(body_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 1. fresh evidence (no drift, safe route) continues to gh pr create
# ---------------------------------------------------------------------------


def test_fresh_evidence_no_drift_continues_to_create_pr(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)

    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(fresh), ""),
    )

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
        )
        assert rc == 0, lines
        assert create_called is True
        assert not any(line.startswith("ERROR=") for line in lines)
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. drift detected blocks
# ---------------------------------------------------------------------------


def test_drift_detected_blocks(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)
    # Fresh re-run has a DIFFERENT decision_inputs_sha256 (drift).
    fresh = fresh_evidence_from_stored(stored, decision_inputs_sha256="sha256:" + "c" * 64)

    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(fresh), ""),
    )

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_DRIFT}" for line in lines), lines
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. evidence file missing blocks
# ---------------------------------------------------------------------------


def test_evidence_file_missing_blocks(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run when evidence is missing")

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_called)

    rc, lines, create_called = _run_main(
        monkeypatch,
        1458,
        [
            "--overlap-preflight-required",
            "--overlap-preflight-evidence-file", "/tmp/does-not-exist-1458-overlap.json",
            "--overlap-preflight-expected-evidence-sha256", "sha256:" + "a" * 64,
            "--overlap-preflight-expected-decision-inputs-sha256", "sha256:" + "b" * 64,
        ],
    )
    assert rc == 2
    assert create_called is False
    assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING}" for line in lines), lines


# ---------------------------------------------------------------------------
# 4. phase/implementation label forces gate even without overlap_preflight args
#    (AC11 — test name matches -k phase_implementation_forced)
# ---------------------------------------------------------------------------


def test_phase_implementation_forced_blocks_without_overlap_preflight_args(
    monkeypatch: pytest.MonkeyPatch,
):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(
        open_pr, "get_linked_issue_labels", lambda repo, issue: ["phase/implementation"]
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run when evidence is missing")

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_called)

    # No --overlap-preflight-* args at all: caller omission must NOT bypass the gate.
    rc, lines, create_called = _run_main(monkeypatch, 1458, [])
    assert rc == 2
    assert create_called is False
    assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING}" for line in lines), lines
    assert any(line == "OVERLAP_PREFLIGHT_FORCED_BY_LABEL=true" for line in lines), lines


def test_no_forcing_without_label_and_without_required_flag_skips_gate(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression parity: no label, no --overlap-preflight-required -> gate inactive,
    existing (pre-#1458) behavior is preserved."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(open_pr, "get_linked_issue_labels", lambda repo, issue: None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("overlap preflight subprocess should not run when gate is inactive")

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_called)

    rc, lines, create_called = _run_main(monkeypatch, 1458, [])
    assert rc == 0, lines
    assert create_called is True


# ---------------------------------------------------------------------------
# 5. unsafe route blocks even with hash match
# ---------------------------------------------------------------------------


def test_unsafe_route_blocks_even_with_hash_match(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        route="wait_for_predecessor",
    )
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)

    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(fresh), ""),
    )

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE}" for line in lines), lines
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6. source.complete=false or source.saturated=true blocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_complete,source_saturated",
    [(False, False), (True, True)],
)
def test_source_degraded_blocks(
    monkeypatch: pytest.MonkeyPatch, source_complete: bool, source_saturated: bool
):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        source_complete=source_complete,
        source_saturated=source_saturated,
    )
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)

    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(fresh), ""),
    )

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE}" for line in lines), lines
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 7. evidence file exists but invalid (parse error / schema mismatch / hash
#    mismatch) blocks with a DIFFERENT error code than "missing"
# ---------------------------------------------------------------------------


def test_evidence_invalid_non_json_blocks(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    handle.write("not json {{{")
    handle.flush()
    handle.close()
    evidence_path = Path(handle.name)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run when evidence parse fails")

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_called)

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", "sha256:" + "a" * 64,
                "--overlap-preflight-expected-decision-inputs-sha256", "sha256:" + "b" * 64,
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID}" for line in lines), lines
        assert not any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING}" for line in lines)
    finally:
        evidence_path.unlink(missing_ok=True)


def test_evidence_invalid_hash_mismatch_blocks(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    # Tamper with stored evidence_sha256 so it no longer matches the recomputed hash.
    stored["evidence_sha256"] = "sha256:" + "0" * 64
    evidence_path = write_evidence_file(stored)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run when evidence hash mismatches")

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_called)

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID}" for line in lines), lines
    finally:
        evidence_path.unlink(missing_ok=True)


def test_evidence_invalid_schema_mismatch_blocks(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    stored["schema"] = "SOME_OTHER_SCHEMA_V1"
    evidence_path = write_evidence_file(stored)

    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: (_ for _ in ()).throw(AssertionError("no subprocess expected")),
    )

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", "sha256:" + "a" * 64,
                "--overlap-preflight-expected-decision-inputs-sha256", "sha256:" + "b" * 64,
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID}" for line in lines), lines
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 8. current_issue.number mismatch blocks
# ---------------------------------------------------------------------------


def test_current_issue_number_mismatch_blocks(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)
    # Fresh evidence's current_issue.number does not match the PR's linked issue.
    fresh = fresh_evidence_from_stored(stored)
    fresh["current_issue"] = {**fresh["current_issue"], "number": 999}

    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(fresh), ""),
    )

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE}" for line in lines), lines
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 9. subprocess timeout / non-JSON output / non-zero exit (auth failure proxy)
#    all fail-closed as E_OVERLAP_PREFLIGHT_SOURCE_FAILURE
# ---------------------------------------------------------------------------


def test_subprocess_timeout_blocks(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)

    def fake_timeout(cmd, **kwargs):
        raise open_pr.subprocess.TimeoutExpired(cmd=cmd, timeout=90)

    monkeypatch.setattr(open_pr.subprocess, "run", fake_timeout)

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_SOURCE_FAILURE}" for line in lines), lines
    finally:
        evidence_path.unlink(missing_ok=True)


def test_subprocess_non_json_output_blocks(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)

    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, "not json output", ""),
    )

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_SOURCE_FAILURE}" for line in lines), lines
    finally:
        evidence_path.unlink(missing_ok=True)


def test_subprocess_nonzero_exit_auth_failure_blocks(monkeypatch: pytest.MonkeyPatch):
    """A non-zero exit (e.g. gh auth failure surfaced as runtime_error by the
    producer script) must fail-closed as E_OVERLAP_PREFLIGHT_SOURCE_FAILURE
    (AC10), regardless of any JSON body on stdout."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)

    error_body = json.dumps({"schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA, "route": "runtime_error", "error": "gh auth failure"})
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(1, error_body, "gh: authentication failed"),
    )

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_SOURCE_FAILURE}" for line in lines), lines
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 10. call-order test: gate runs after existing-PR-check/dry-run, before create_pr
# ---------------------------------------------------------------------------


def test_gate_runs_after_existing_pr_check_and_before_create_pr(monkeypatch: pytest.MonkeyPatch):
    call_order: list[str] = []

    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
    monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-1458-test")
    monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
    monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
    monkeypatch.setattr(
        open_pr,
        "_run_pr_body_validator",
        lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
    )
    monkeypatch.setattr(
        open_pr,
        "_run_japanese_content_validator",
        lambda body_text, threshold=0.1: {
            "status": "pass",
            "failed_blocks": 0,
            "aggregate_ratio": 0.5,
            "threshold": 0.1,
            "body_sha256": "",
            "stderr": "",
        },
    )

    def fake_find_existing_pr(repo, branch):
        call_order.append("find_existing_pr")
        return None

    monkeypatch.setattr(open_pr, "find_existing_pr", fake_find_existing_pr)

    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)

    def fake_run_overlap_preflight_gate(**kwargs):
        call_order.append("run_overlap_preflight_gate")
        return True, None, "", fresh

    monkeypatch.setattr(open_pr, "run_overlap_preflight_gate", fake_run_overlap_preflight_gate)

    def fake_create_pr(*args, **kwargs):
        call_order.append("create_pr")
        return "https://github.com/squne121/loop-protocol/pull/9999"

    monkeypatch.setattr(open_pr, "create_pr", fake_create_pr)

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
            patch_create_pr=False,
        )
        assert rc == 0, lines
        assert call_order == ["find_existing_pr", "run_overlap_preflight_gate", "create_pr"], call_order
    finally:
        evidence_path.unlink(missing_ok=True)


def test_dry_run_skips_overlap_gate_entirely(monkeypatch: pytest.MonkeyPatch):
    """dry-run returns before the gate; the gate subprocess must never run."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(
        open_pr, "get_linked_issue_labels", lambda repo, issue: ["phase/implementation"]
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("overlap preflight gate must not run in dry-run mode")

    monkeypatch.setattr(open_pr, "run_overlap_preflight_gate", fail_if_called)

    rc, lines, create_called = _run_main(monkeypatch, 1458, ["--dry-run"])
    assert rc == 0, lines
    assert create_called is False
    assert any(line == "DRY_RUN=true" for line in lines)


# ---------------------------------------------------------------------------
# 11. --repo consistency (AC8): the repo used for the online re-run subprocess
#     matches the repo used for `gh pr create`.
# ---------------------------------------------------------------------------


def test_overlap_check_repo_matches_pr_create_repo(monkeypatch: pytest.MonkeyPatch):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)

    observed_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        observed_cmds.append(cmd)
        return FakeCompletedProcess(0, json.dumps(fresh), "")

    monkeypatch.setattr(open_pr.subprocess, "run", fake_run)

    observed_create_pr_repo = {"repo": None}

    def fake_create_pr(repo, title, body_file, branch, draft):
        observed_create_pr_repo["repo"] = repo
        return "https://github.com/squne121/loop-protocol/pull/9999"

    monkeypatch.setattr(open_pr, "create_pr", fake_create_pr)

    try:
        rc, lines, _ = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                "--overlap-preflight-expected-decision-inputs-sha256", stored["decision_inputs_sha256"],
            ],
            patch_create_pr=False,
        )
        assert rc == 0, lines
        overlap_check_cmd = observed_cmds[0]
        repo_flag_index = overlap_check_cmd.index("--repo")
        overlap_check_repo = overlap_check_cmd[repo_flag_index + 1]
        assert overlap_check_repo == observed_create_pr_repo["repo"] == "squne121/loop-protocol"
    finally:
        evidence_path.unlink(missing_ok=True)


def test_run_overlap_preflight_gate_signature_uses_keyword_only_args():
    """Guard against accidental positional-arg drift for a security-relevant gate."""
    import inspect

    sig = inspect.signature(open_pr.run_overlap_preflight_gate)
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, f"{name} must be keyword-only"
