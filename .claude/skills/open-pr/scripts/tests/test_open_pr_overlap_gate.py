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

# Issue #1470: captured at collection time (before any monkeypatching), so
# tests that need the REAL resolve_canonical_repository() behavior (mixed
# case / rename alias resolution) can restore it after _common_monkeypatches
# installs its default identity mock.
_REAL_RESOLVE_CANONICAL_REPOSITORY = open_pr.resolve_canonical_repository


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
    source_limit: object = 500,
    validation_errors: dict | None = None,
    unresolved_refs: list | None = None,
    blocking_predecessor: object | None = None,
    repository: str = "squne121/loop-protocol",
    include_repository: bool = True,
) -> dict:
    """Build a stored evidence dict with a correctly-computed embedded
    evidence_sha256 (mirrors check_implementation_overlap.py's build_evidence
    canonicalization contract).

    Issue #1470: ``repository`` is included by default (matching the
    canonicalized repo used across these tests). ``include_repository=False``
    builds a legacy V1-shaped evidence dict (no ``repository`` key at all)
    with a *correctly computed* embedded hash for that legacy shape, so tests
    can verify that the required-field validation (not hash mismatch) is what
    rejects it.
    """
    body = {
        "schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA,
        "current_issue": {"number": current_issue_number, "allowed_paths": []},
        "source": {
            "complete": source_complete,
            "saturated": source_saturated,
            "limit": source_limit,
            "collected_at": "2026-07-11T00:00:00Z",
        },
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
    if include_repository:
        body["repository"] = repository
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
    # Default: fresh label re-fetch succeeds with no labels (no phase/implementation
    # forcing). Individual tests override this to simulate forcing / fetch failure.
    monkeypatch.setattr(
        open_pr, "fetch_current_linked_issue_labels", lambda repo, issue: ([], None)
    )
    # Issue #1470: default identity canonicalization (no real GitHub API call).
    # resolve_repo() above already returns a lowercase canonical value, so the
    # identity mapping keeps pre-#1470 test expectations unchanged. Tests that
    # specifically exercise canonicalization (mixed-case / rename alias)
    # restore _REAL_RESOLVE_CANONICAL_REPOSITORY or override this explicitly.
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", lambda repo: repo)


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


def test_stored_limit_is_forwarded_to_online_preflight(monkeypatch: pytest.MonkeyPatch):
    """GIVEN verified stored limit WHEN rechecking THEN the same --limit is used."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        source_limit=500,
    )
    evidence_path = write_evidence_file(stored)
    observed_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        observed_cmds.append(cmd)
        return FakeCompletedProcess(0, json.dumps(fresh_evidence_from_stored(stored)), "")

    monkeypatch.setattr(open_pr.subprocess, "run", fake_run)

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
        command = observed_cmds[0]
        limit_index = command.index("--limit")
        assert command[limit_index + 1] == "500"
    finally:
        evidence_path.unlink(missing_ok=True)


@pytest.mark.parametrize("source_limit", [None, "500", True, 0, -1])
def test_invalid_stored_limit_blocks_before_online_recheck(
    monkeypatch: pytest.MonkeyPatch, source_limit: object
):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        source_limit=source_limit,
    )
    evidence_path = write_evidence_file(stored)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("invalid stored limit must block before online recheck")

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
        assert any(
            line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID}" for line in lines
        ), lines
    finally:
        evidence_path.unlink(missing_ok=True)


@pytest.mark.parametrize("fresh_limit", [None, "500", True, 0, -1, 499])
def test_invalid_or_mismatched_fresh_limit_blocks_pr_creation(
    monkeypatch: pytest.MonkeyPatch, fresh_limit: object
):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        source_limit=500,
    )
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)
    fresh["source"]["limit"] = fresh_limit
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
# 2b. P2-1 provenance chain: stored.decision_inputs_sha256 must be connected
#     to expected_decision_inputs_sha256 BEFORE any fresh comparison. Without
#     this, a stored artifact from a DIFFERENT preflight collection chain
#     (decision_inputs_sha256=D1) could pass if the caller's expected value
#     (D2) happens to match the fresh online re-run's result (also D2) even
#     though stored and fresh never shared the same collection chain.
# ---------------------------------------------------------------------------


def test_decision_inputs_provenance_chain_blocks_on_stored_expected_mismatch(
    monkeypatch: pytest.MonkeyPatch,
):
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    d1 = "sha256:" + "d1" * 32
    d2 = "sha256:" + "d2" * 32
    # stored artifact belongs to a collection chain with decision_inputs_sha256=D1
    stored = build_stored_evidence(decision_inputs_sha256=d1, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)

    def fail_if_called(*args, **kwargs):
        raise AssertionError(
            "subprocess should not run before the stored/expected "
            "decision_inputs_sha256 provenance check"
        )

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_called)

    try:
        rc, lines, create_called = _run_main(
            monkeypatch,
            1458,
            [
                "--overlap-preflight-required",
                "--overlap-preflight-evidence-file", str(evidence_path),
                "--overlap-preflight-expected-evidence-sha256", stored["evidence_sha256"],
                # caller-supplied expected value is D2 (a DIFFERENT collection chain
                # from the stored artifact's D1), even though a fresh online re-run
                # would also report D2 (simulated by fail_if_called never running).
                "--overlap-preflight-expected-decision-inputs-sha256", d2,
            ],
        )
        assert rc == 2
        assert create_called is False
        assert any(
            line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID}" for line in lines
        ), lines
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
        open_pr,
        "fetch_current_linked_issue_labels",
        lambda repo, issue: (["phase/implementation"], None),
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
    """Regression parity: fresh fetch succeeds with an empty label list, no
    --overlap-preflight-required -> gate inactive, existing (pre-#1458)
    behavior is preserved."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(
        open_pr, "fetch_current_linked_issue_labels", lambda repo, issue: ([], None)
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("overlap preflight subprocess should not run when gate is inactive")

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_called)

    rc, lines, create_called = _run_main(monkeypatch, 1458, [])
    assert rc == 0, lines
    assert create_called is True


def test_labels_fetch_failure_fails_closed_blocks_pr_creation(monkeypatch: pytest.MonkeyPatch):
    """P1-1: if the fresh labels re-fetch fails (auth error / bad JSON / type
    mismatch / etc.), it must be treated as fail-closed (gate forced active),
    never as 'no label' (fail-open)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(
        open_pr,
        "fetch_current_linked_issue_labels",
        lambda repo, issue: (None, "gh issue view 失敗: simulated auth error"),
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run when evidence is missing")

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_called)

    rc, lines, create_called = _run_main(monkeypatch, 1458, [])
    assert rc == 2
    assert create_called is False
    assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING}" for line in lines), lines
    assert any(line == "OVERLAP_PREFLIGHT_FORCED_BY_LABEL=true" for line in lines), lines
    assert any(
        line.startswith("OVERLAP_PREFLIGHT_LABELS_FETCH_ERROR=") for line in lines
    ), lines


def test_toctou_label_added_after_initial_fetch_still_forces_gate(
    monkeypatch: pytest.MonkeyPatch,
):
    """P1-1 (TOCTOU): the gate-activation decision must always use a fresh
    re-fetch taken immediately before the decision, never an earlier cached
    value. This simulates a label added to the linked issue *after* any
    earlier (unrelated) issue lookup but *before* PR creation: the fresh
    re-fetch must observe it and force the gate."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    # get_linked_issue_state (an unrelated, earlier lookup) reports no
    # knowledge of labels at all; only the fresh re-fetch right before the
    # gate decision sees the label that was added in the mutation window.
    monkeypatch.setattr(
        open_pr,
        "fetch_current_linked_issue_labels",
        lambda repo, issue: (["phase/implementation"], None),
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run when evidence is missing")

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_called)

    rc, lines, create_called = _run_main(monkeypatch, 1458, [])
    assert rc == 2
    assert create_called is False
    assert any(line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING}" for line in lines), lines
    assert any(line == "OVERLAP_PREFLIGHT_FORCED_BY_LABEL=true" for line in lines), lines


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

    error_body = json.dumps(
        {"schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA, "route": "runtime_error", "error": "gh auth failure"}
    )
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
    monkeypatch.setattr(
        open_pr, "fetch_current_linked_issue_labels", lambda repo, issue: ([], None)
    )
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", lambda repo: repo)

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
        open_pr,
        "fetch_current_linked_issue_labels",
        lambda repo, issue: (["phase/implementation"], None),
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


# ---------------------------------------------------------------------------
# 12. #1477 fixed overlap_readback_waiver
# ---------------------------------------------------------------------------


def _fixed_overlap_readback_waiver() -> dict:
    return {
        "issue_numbers": [519, 520, 1429],
        "reason": "human_approved_readback_ignore",
        "expires_on": "2026-07-13",
        "approved_by": "user_session",
    }


def _readback_incomplete_candidate(number: int) -> dict:
    return {
        "issue_number": number,
        "readback_complete": False,
        "reasons": ["readback_incomplete_missing_outcome_or_in_scope"],
    }


def _waiver_live_body() -> str:
    return """## Machine-Readable Contract

```yaml
overlap_readback_waiver:
  issue_numbers: [519, 520, 1429]
  reason: human_approved_readback_ignore
  expires_on: \"2026-07-13\"
  approved_by: user_session
```
"""


def _snapshot_comment(
    body_sha256: str,
    *,
    status: str = "go",
    comment_id: int = 1,
    created_at: str = "2026-07-13T00:00:00Z",
    trusted: bool = True,
) -> dict:
    author_id = 63350259 if trusted else 999999
    author = "squne121" if trusted else "outside"
    return {
        "id": comment_id,
        "html_url": f"https://github.com/squne121/loop-protocol/issues/1477#issuecomment-{comment_id}",
        "created_at": created_at,
        "updated_at": created_at,
        "author": author,
        "author_id": author_id,
        "author_type": "User",
        "author_association": "OWNER",
        "body": f"""```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: {status}
  generated_at: \"{created_at}\"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/loop-protocol/issues/1477
  body_sha256: \"{body_sha256}\"
```
""",
    }


def _patch_live_waiver_readback(monkeypatch, body: str, comments: list[dict], error: str | None = None) -> None:
    payload = {
        "body": body,
        "url": "https://github.com/squne121/loop-protocol/issues/1477",
    }
    monkeypatch.setattr(
        open_pr,
        "run_gh",
        lambda *args, **kwargs: FakeCompletedProcess(0, json.dumps(payload), ""),
    )
    monkeypatch.setattr(
        open_pr.contract_review_parser,
        "fetch_issue_comments",
        lambda issue, repo: (comments, error),
    )


def test_overlap_readback_waiver_allows_only_the_fixed_incomplete_candidates(monkeypatch):
    """GIVEN verified fixed waiver WHEN only its three targets are incomplete
    THEN the effective safety predicate can proceed."""
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1477,
    )
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)
    fresh["route"] = "human_review_required"
    fresh["candidates"] = [_readback_incomplete_candidate(number) for number in (519, 520, 1429)]
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(fresh), ""),
    )
    monkeypatch.setattr(
        open_pr,
        "_load_verified_overlap_readback_waiver",
        lambda repo, linked_issue: (_fixed_overlap_readback_waiver(), None),
    )

    try:
        ok, error, detail, effective = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1477,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        assert ok is True, (error, detail)
        assert effective is not None
        assert effective["route"] == "proceed_with_collision_evidence"
    finally:
        evidence_path.unlink(missing_ok=True)


def test_overlap_readback_waiver_rejects_non_target_or_non_readback_blocker(monkeypatch):
    """対象外 Issue と readback_incomplete 以外の理由は waiver で通さない。"""
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1477,
    )
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)
    fresh["route"] = "human_review_required"
    fresh["candidates"] = [
        _readback_incomplete_candidate(519),
        _readback_incomplete_candidate(520),
        {"issue_number": 999, "readback_complete": False, "reasons": ["readback_incomplete_x"]},
    ]
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(fresh), ""),
    )
    monkeypatch.setattr(
        open_pr,
        "_load_verified_overlap_readback_waiver",
        lambda repo, linked_issue: (_fixed_overlap_readback_waiver(), None),
    )

    try:
        ok, error, _, _ = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1477,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        assert ok is False
        assert error == open_pr.E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE
    finally:
        evidence_path.unlink(missing_ok=True)


def test_overlap_readback_waiver_rejects_another_complete_candidate_blocker(monkeypatch):
    """readback 完了済みでも C3 なら waiver 単独の原因とは扱わない。"""
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1477,
    )
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)
    fresh["route"] = "human_review_required"
    fresh["candidates"] = [
        *[_readback_incomplete_candidate(number) for number in (519, 520, 1429)],
        {"issue_number": 777, "readback_complete": True, "policy_class": "C3", "reasons": ["collision"]},
    ]
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(fresh), ""),
    )
    monkeypatch.setattr(
        open_pr,
        "_load_verified_overlap_readback_waiver",
        lambda repo, linked_issue: (_fixed_overlap_readback_waiver(), None),
    )

    try:
        ok, error, _, _ = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1477,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        assert ok is False
        assert error == open_pr.E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE
    finally:
        evidence_path.unlink(missing_ok=True)


def test_overlap_readback_waiver_requires_live_body_snapshot_integrity(monkeypatch):
    """live body SHA と一致する go snapshot がない waiver は fail-closed。"""
    body = _waiver_live_body()
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, body, [_snapshot_comment(sha)])

    waiver, error = open_pr._load_verified_overlap_readback_waiver(
        "squne121/loop-protocol", 1477, today=open_pr.date(2026, 7, 13)
    )
    assert error is None
    assert waiver == _fixed_overlap_readback_waiver()

    _patch_live_waiver_readback(
        monkeypatch, body, [_snapshot_comment("sha256:" + "0" * 64)]
    )
    waiver, error = open_pr._load_verified_overlap_readback_waiver(
        "squne121/loop-protocol", 1477, today=open_pr.date(2026, 7, 13)
    )
    assert waiver is None
    assert error is not None


def test_overlap_readback_waiver_rejects_expired_or_changed_contract(monkeypatch):
    body = _waiver_live_body()
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, body, [_snapshot_comment(sha)])

    waiver, error = open_pr._load_verified_overlap_readback_waiver(
        "squne121/loop-protocol", 1477, today=open_pr.date(2026, 7, 14)
    )
    assert waiver is None
    assert "期限" in error


@pytest.mark.parametrize(
    ("comments", "expected_valid"),
    [
        (
            [
                _snapshot_comment("BODY", status="go", comment_id=10),
                _snapshot_comment("BODY", status="blocked", comment_id=11),
            ],
            False,
        ),
        (
            [
                _snapshot_comment("BODY", status="go", comment_id=20, trusted=False),
                _snapshot_comment("BODY", status="blocked", comment_id=21),
            ],
            False,
        ),
        (
            [
                _snapshot_comment("BODY", status="blocked", comment_id=30),
                _snapshot_comment("BODY", status="go", comment_id=31),
            ],
            True,
        ),
    ],
)
def test_overlap_readback_waiver_uses_latest_trusted_snapshot_precedence(
    monkeypatch, comments, expected_valid
):
    body = _waiver_live_body()
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    for comment in comments:
        comment["body"] = comment["body"].replace("BODY", sha)
    _patch_live_waiver_readback(monkeypatch, body, comments)

    waiver, error = open_pr._load_verified_overlap_readback_waiver(
        "squne121/loop-protocol", 1477, today=open_pr.date(2026, 7, 13)
    )

    assert (waiver is not None) is expected_valid
    assert (error is None) is expected_valid


def test_overlap_readback_waiver_uses_comment_id_to_break_same_timestamp_tie(monkeypatch):
    body = _waiver_live_body()
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    same_time = "2026-07-13T00:00:00Z"
    comments = [
        _snapshot_comment(sha, status="blocked", comment_id=50, created_at=same_time),
        _snapshot_comment(sha, status="go", comment_id=51, created_at=same_time),
    ]
    _patch_live_waiver_readback(monkeypatch, body, comments)

    waiver, error = open_pr._load_verified_overlap_readback_waiver(
        "squne121/loop-protocol", 1477, today=open_pr.date(2026, 7, 13)
    )

    assert error is None
    assert waiver == _fixed_overlap_readback_waiver()


def test_overlap_readback_waiver_requires_complete_paginated_comment_readback(monkeypatch):
    body = _waiver_live_body()
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    comments = [{"id": index, "body": "unrelated"} for index in range(1, 101)]
    comments.append(_snapshot_comment(sha, comment_id=101))
    _patch_live_waiver_readback(monkeypatch, body, comments)

    waiver, error = open_pr._load_verified_overlap_readback_waiver(
        "squne121/loop-protocol", 1477, today=open_pr.date(2026, 7, 13)
    )

    assert error is None
    assert waiver == _fixed_overlap_readback_waiver()

    _patch_live_waiver_readback(monkeypatch, body, [], error="comments_fetch_incomplete")
    waiver, error = open_pr._load_verified_overlap_readback_waiver(
        "squne121/loop-protocol", 1477, today=open_pr.date(2026, 7, 13)
    )
    assert waiver is None
    assert "不完全" in error


def test_overlap_readback_waiver_is_bound_to_1477_in_the_canonical_repository(monkeypatch):
    monkeypatch.setattr(open_pr, "run_gh", lambda *args, **kwargs: pytest.fail("must not read"))

    waiver, error = open_pr._load_verified_overlap_readback_waiver(
        "other/repository", 1477
    )
    assert waiver is None
    assert "固定 binding" in error

    waiver, error = open_pr._load_verified_overlap_readback_waiver(
        "squne121/loop-protocol", 9999
    )
    assert waiver is None
    assert "固定 binding" in error


# ---------------------------------------------------------------------------
# 13. Issue #1470 — repository binding (canonical full_name enforcement)
# ---------------------------------------------------------------------------


def test_canonical_repo_is_shared_by_preflight_and_pr_create(monkeypatch: pytest.MonkeyPatch):
    """GIVEN a requested repo that resolves to a canonical full_name WHEN the
    overlap gate runs THEN the online recheck subprocess --repo and gh pr
    create --repo both receive the SAME resolved canonical value (AC1)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "SQUNE121/LOOP-PROTOCOL")
    monkeypatch.setattr(
        open_pr, "resolve_canonical_repository", lambda repo: "squne121/loop-protocol"
    )
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        repository="squne121/loop-protocol",
    )
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
        repo_flag_index = observed_cmds[0].index("--repo")
        overlap_check_repo = observed_cmds[0][repo_flag_index + 1]
        assert overlap_check_repo == "squne121/loop-protocol"
        assert observed_create_pr_repo["repo"] == "squne121/loop-protocol"
    finally:
        evidence_path.unlink(missing_ok=True)


def test_stored_repository_missing_blocks_before_online_recheck(monkeypatch: pytest.MonkeyPatch):
    """GIVEN legacy V1 stored evidence (no repository key, correctly-computed
    legacy hash) WHEN the gate runs THEN it is rejected before the online
    recheck subprocess (AC2/AC5)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        include_repository=False,
    )
    evidence_path = write_evidence_file(stored)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run when stored repository is missing")

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


def test_stored_repository_invalid_type_blocks_before_online_recheck(monkeypatch: pytest.MonkeyPatch):
    """GIVEN stored evidence whose repository field is a non-string type
    WHEN the gate runs THEN it is rejected before the online recheck
    subprocess (AC2)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
    )
    stored["repository"] = 12345
    body_for_hash = {k: v for k, v in stored.items() if k != "evidence_sha256"}
    stored["evidence_sha256"] = f"sha256:{_sha256(_canonical_json(body_for_hash))}"
    evidence_path = write_evidence_file(stored)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run when stored repository type is invalid")

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


def test_stored_repository_mismatch_blocks_before_online_recheck(monkeypatch: pytest.MonkeyPatch):
    """GIVEN stored evidence whose repository field does not match the
    canonical target WHEN the gate runs THEN it is rejected before the online
    recheck subprocess (AC2)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        repository="someone-else/other-repo",
    )
    evidence_path = write_evidence_file(stored)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run when stored repository mismatches target")

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


def test_fresh_repository_missing_blocks_before_pr_creation(monkeypatch: pytest.MonkeyPatch):
    """GIVEN fresh (online re-run) evidence with no repository field WHEN the
    gate runs THEN it is rejected before gh pr create (AC3)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)
    del fresh["repository"]

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


def test_fresh_repository_mismatch_blocks_before_pr_creation(monkeypatch: pytest.MonkeyPatch):
    """GIVEN fresh (online re-run) evidence whose repository field does not
    match the canonical target WHEN the gate runs THEN it is rejected before
    gh pr create (AC3)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored, repository="attacker/other-repo")

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


def test_mixed_case_requested_repo_uses_canonical_repository(monkeypatch: pytest.MonkeyPatch):
    """GIVEN a requested repo with mixed-case owner/name segments WHEN
    resolved THEN the canonical lowercase full_name is used for both the
    online recheck and gh pr create (AC4)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", _REAL_RESOLVE_CANONICAL_REPOSITORY)
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "SQUNE121/LOOP-PROTOCOL")
    monkeypatch.setattr(
        open_pr,
        "run_gh",
        lambda *args, **kwargs: FakeCompletedProcess(
            0, json.dumps({"full_name": "squne121/loop-protocol"}), ""
        ),
    )
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        repository="squne121/loop-protocol",
    )
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)

    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(fresh), ""),
    )

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
        assert observed_create_pr_repo["repo"] == "squne121/loop-protocol"
    finally:
        evidence_path.unlink(missing_ok=True)


def test_renamed_repository_alias_uses_resolved_full_name(monkeypatch: pytest.MonkeyPatch):
    """GIVEN a requested repo that is a stale post-rename/transfer alias WHEN
    resolved THEN the GitHub API's current full_name is used as the canonical
    target, and stored evidence built under the NEW name is accepted (AC4)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", _REAL_RESOLVE_CANONICAL_REPOSITORY)
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/old-repo-name")
    monkeypatch.setattr(
        open_pr,
        "run_gh",
        lambda *args, **kwargs: FakeCompletedProcess(
            0, json.dumps({"full_name": "squne121/new-repo-name"}), ""
        ),
    )
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        repository="squne121/new-repo-name",
    )
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored)

    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(fresh), ""),
    )

    observed_create_pr_repo = {"repo": None}

    def fake_create_pr(repo, title, body_file, branch, draft):
        observed_create_pr_repo["repo"] = repo
        return "https://github.com/squne121/new-repo-name/pull/9999"

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
        assert observed_create_pr_repo["repo"] == "squne121/new-repo-name"
    finally:
        evidence_path.unlink(missing_ok=True)


def test_repository_binding_precedes_generic_decision_hash_drift(monkeypatch: pytest.MonkeyPatch):
    """GIVEN fresh evidence whose repository field mismatches the canonical
    target, but whose decision_inputs_sha256 is UNCHANGED from stored's (i.e.
    the generic hash-drift check alone would pass) WHEN the gate runs THEN
    the explicit repository binding check still rejects it, and the error
    detail attributes the block to the repository mismatch (not the decision
    hash) (AC5 ordering)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    stored = build_stored_evidence(decision_inputs_sha256="sha256:" + "b" * 64, current_issue_number=1458)
    evidence_path = write_evidence_file(stored)
    fresh = fresh_evidence_from_stored(stored, repository="attacker/other-repo")

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
        detail_line = next(line for line in lines if line.startswith("ERROR_DETAIL="))
        assert "repository" in detail_line
        assert "decision_inputs_sha256 drift" not in detail_line
    finally:
        evidence_path.unlink(missing_ok=True)


def test_cross_repo_same_issue_number_is_rejected(monkeypatch: pytest.MonkeyPatch):
    """GIVEN repository A's stored evidence WHEN reused for the same-numbered
    issue while the PR mutation target resolves to repository B THEN the gate
    rejects it and gh pr create is never invoked (AC7)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "owner-b/repo-b")
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", lambda repo: repo)
    stored = build_stored_evidence(
        decision_inputs_sha256="sha256:" + "b" * 64,
        current_issue_number=1458,
        repository="owner-a/repo-a",
    )
    evidence_path = write_evidence_file(stored)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not run for cross-repo evidence reuse")

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
