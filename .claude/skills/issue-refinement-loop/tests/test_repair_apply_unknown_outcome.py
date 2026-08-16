"""Behavioral tests for AC5 (post-dispatch retry-budget separation and
authoritative-readback digest classification) of `run_repair_action_apply()`
(Issue #2039 AC1/AC4/AC5/AC6).

GIVEN a PATCH dispatch whose executor could not itself confirm the outcome,
WHEN the consumer resolves the result, THEN it must NEVER blind-retry the
mutation (post_dispatch_retry_budget stays 0, the transaction closure is
called exactly once), and must classify the live digest as
candidate/old/third via a single authoritative read -- never guessing.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import run_refinement_preflight as rrp  # noqa: E402
from run_refinement_preflight import (  # noqa: E402
    _classify_repair_apply_readback_digest,
    _repair_receipt_from_txn_result,
)

_SCHEMA = json.loads((_SKILL_ROOT / "schemas" / "repair_apply_result_v1.schema.json").read_text(encoding="utf-8"))

ORIGINAL_BODY = "original body\n"
REPAIRED_BODY = "repaired body\n"


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_candidate(tmp_path: Path, *, issue_number: int = 2039) -> Path:
    artifact_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = artifact_dir / "candidate_body.md"
    candidate_path.write_text(REPAIRED_BODY)
    repair_action = {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": _hex(ORIGINAL_BODY),
        "repaired_body_sha256": _hex(REPAIRED_BODY),
        "diagnostics_artifact": None,
        "candidate_body_artifact": str(candidate_path),
        "repair_kinds": [],
        "reason_codes": [],
    }
    preflight_result = {
        "schema": "issue_refinement_preflight_result/v1",
        "repair_action": repair_action,
        "original_updated_at": "2024-01-01T00:00:00Z",
        "result_core_sha256": "sha256:testrun",
        "source_lane": "unanchored",
    }
    result_path = artifact_dir / "preflight_result.json"
    result_path.write_text(json.dumps(preflight_result))
    return result_path


class RecordingApplyTransaction:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, current_issue: dict, candidate_body: str) -> dict:
        self.calls += 1
        return self.result


# ---------------------------------------------------------------------------
# Unit-level classification tests
# ---------------------------------------------------------------------------


def test_classify_readback_digest_candidate_when_remote_matches_candidate():
    assert _classify_repair_apply_readback_digest("sha256:abc", "abc", "def") == "candidate"


def test_classify_readback_digest_old_when_remote_matches_old():
    assert _classify_repair_apply_readback_digest("sha256:def", "abc", "def") == "old"


def test_classify_readback_digest_third_when_remote_matches_neither():
    assert _classify_repair_apply_readback_digest("sha256:zzz", "abc", "def") == "third"


def test_classify_readback_digest_unknown_when_remote_missing():
    assert _classify_repair_apply_readback_digest(None, "abc", "def") == "unknown"


def test_receipt_resolve_readback_not_called_for_confirmed_outcomes():
    """AC5: resolve_readback is called ONLY to disambiguate an unknown
    outcome, not for already-confirmed not_attempted/no_change/applied."""
    calls = []

    def _resolve():
        calls.append(None)
        return "sha256:should-not-be-used"

    receipt = _repair_receipt_from_txn_result(
        {"status": "no_change"}, candidate_digest="abc", old_digest="def", resolve_readback=_resolve
    )
    assert receipt["mutation_outcome"] == "no_change"
    assert calls == []


# ---------------------------------------------------------------------------
# End-to-end retry-budget / blind-retry-avoidance tests
# ---------------------------------------------------------------------------


def test_retry_budget_is_always_zero_and_never_blind_retries(tmp_path: Path) -> None:
    """AC5: post_dispatch_retry_budget/retries_used stay 0, and the
    transaction closure is invoked exactly ONCE even when the outcome is
    unknown -- proving no blind retry occurred."""
    result_path = _write_candidate(tmp_path)
    apply_txn = RecordingApplyTransaction({"status": "mutation_outcome_unknown", "errors": []})

    def _fetch():
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=apply_txn,
    )

    jsonschema.validate(result, _SCHEMA)
    assert apply_txn.calls == 1
    assert result["retry"] == {"post_dispatch_retry_budget": 0, "retries_used": 0}
    assert result["mutation_outcome"] == "unknown"
    assert result["phase"] != "complete"


def test_unknown_outcome_authoritative_readback_classifies_old_when_body_unchanged(tmp_path: Path) -> None:
    """AC5: when the executor's outcome is unknown and the post-dispatch
    authoritative read shows the body is STILL the pre-dispatch body, the
    receipt classifies it as `old` (nothing actually changed) rather than
    silently promoting mutation_outcome to no_change/applied."""
    result_path = _write_candidate(tmp_path)
    apply_txn = RecordingApplyTransaction({"status": "mutation_outcome_unknown", "errors": []})

    # Same body reported by every fetch() call (initial + the AC5 readback):
    # nothing actually changed server-side.
    def _fetch():
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=apply_txn,
    )

    jsonschema.validate(result, _SCHEMA)
    # mutation_outcome MUST stay `unknown` -- the receipt digest_class merely
    # informs a human/consumer, it never overrides the lossless projection.
    assert result["mutation_outcome"] == "unknown"
    assert result["receipt"]["final_readback"]["status"] == "verified"
    assert result["receipt"]["final_readback"]["digest_class"] == "old"


# ---------------------------------------------------------------------------
# Issue #2039 P0-4: canonical (nested) ISSUE_EDIT_TXN_RESULT_V1 receipt shape
# ---------------------------------------------------------------------------
#
# edit_issue_txn.py's real `_render_result()` nests the attempted/outcome
# fields the receipt adapter needs under `body_update` and `content_update`;
# only `status`/`mutation_started`/`errors` live at the top level. A prior
# version of `_repair_receipt_from_txn_result()` read a nonexistent
# top-level `body_attempted` / `remote_current_body_sha256`, which always
# missed against a real (nested) receipt and silently degraded
# `patch_attempted` to False -- skipping AC9 fresh validation exactly when
# the executor reported `mutation_outcome_unknown` (mutation_started=False,
# body_update.attempted=True, content_update.patch_attempted=True,
# content_update.mutation_outcome="unknown"). These tests exercise the real
# nested shape directly (never an invented flat one) so this regression
# cannot silently reappear.


def test_receipt_reads_nested_body_and_content_update_not_top_level_flat_keys():
    """Issue #2039 P0-4: `patch_attempted` and the remote digest must come
    from the nested `body_update`/`content_update` objects of the real
    ISSUE_EDIT_TXN_RESULT_V1 shape, not from nonexistent top-level flat
    keys."""
    canonical_unknown_receipt = {
        "schema": "issue_edit_txn_result/v1",
        "status": "mutation_outcome_unknown",
        "mutation_started": False,
        "body_update": {
            "attempted": True,
            "status": "failed",
            "previous_body_sha256": "sha256:old",
            "new_body_sha256": "sha256:candidate",
            "remote_current_body_sha256": "sha256:live-refreshed",
            "artifact_ref": "artifacts/2039/issue-metadata/x.input.json",
        },
        "content_update": {
            "previous_title": None,
            "requested_title": None,
            "remote_current_title": None,
            "patch_attempted": True,
            "mutation_outcome": "unknown",
        },
        "errors": [],
    }

    receipt = _repair_receipt_from_txn_result(
        canonical_unknown_receipt, candidate_digest="candidate", old_digest="old"
    )

    # Nested body_update.attempted / content_update.patch_attempted must be
    # honored -- a top-level-only reader would silently see False here.
    assert receipt["patch_attempted"] is True
    assert receipt["mutation_outcome"] == "unknown"
    assert receipt["failure_code"] == "final_readback_unresolvable"
    # Nested body_update.remote_current_body_sha256 must be honored -- a
    # top-level-only reader would see None and fall through to an
    # unresolved readback even though the executor actually reported a
    # digest.
    assert receipt["final_readback"]["digest"] == "sha256:live-refreshed"
    assert receipt["final_readback"]["status"] == "verified"


def test_receipt_top_level_flat_keys_are_ignored_when_absent_from_nested_shape():
    """A receipt that ONLY carries invented top-level flat keys (no
    body_update/content_update at all) must be treated as patch NOT
    attempted -- those top-level keys never exist on a real
    ISSUE_EDIT_TXN_RESULT_V1 receipt, so honoring them would be reading a
    shape edit_issue_txn.py never actually emits."""
    flat_only_stub = {
        "status": "ok",
        "body_attempted": True,
        "remote_current_body_sha256": "sha256:should-be-ignored",
        "errors": [],
    }

    receipt = _repair_receipt_from_txn_result(flat_only_stub, candidate_digest="abc", old_digest="def")

    assert receipt["patch_attempted"] is False
    assert receipt["final_readback"]["digest"] is None
    assert receipt["final_readback"]["status"] == "unresolved"


def test_mutation_outcome_unknown_with_patch_attempted_does_not_skip_fresh_validation(tmp_path: Path) -> None:
    """Issue #2039 P0-4 / AC9: when the canonical (nested) receipt reports
    content_update.patch_attempted=True under a mutation_outcome_unknown
    status, fresh validation must actually run (status != "not_run"), never
    be silently skipped because the adapter misread patch_attempted as
    False."""
    result_path = _write_candidate(tmp_path)
    canonical_unknown_txn_result = {
        "status": "mutation_outcome_unknown",
        "mutation_started": False,
        "body_update": {
            "attempted": True,
            "status": "failed",
            "remote_current_body_sha256": f"sha256:{_hex(ORIGINAL_BODY)}",
        },
        "content_update": {
            "patch_attempted": True,
            "mutation_outcome": "unknown",
        },
        "errors": [],
    }
    apply_txn = RecordingApplyTransaction(canonical_unknown_txn_result)

    fresh_validate_calls: list[str] = []

    def _fresh_validate(body: str) -> dict:
        fresh_validate_calls.append(body)
        return {"actionable_repair": False, "source_lane": "unanchored", "error": None}

    def _fetch():
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=apply_txn,
        fresh_validate=_fresh_validate,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["receipt"]["patch_attempted"] is True
    assert result["mutation_outcome"] == "unknown"
    # The regression: fresh_validation must NOT stay "not_run" when a patch
    # was actually attempted, even though the overall outcome is unknown.
    assert result["fresh_validation"]["status"] != "not_run"
    assert fresh_validate_calls, "fresh_validate producer must actually be invoked, not skipped"


# ---------------------------------------------------------------------------
# PR #2202 review fix-delta (P0-6): timeout/unknown recovery safety of the
# DEFAULT `apply_transaction` (the real subprocess-calling path, exercised
# with apply_transaction=None -- never the injectable RecordingApplyTransaction
# seam the tests above use, since that seam bypasses the subprocess.run()
# calls this fix actually changes).
# ---------------------------------------------------------------------------


def _readiness_stdout_ok() -> str:
    return json.dumps({"status": "pass", "body_sha256": "sha256:x", "source_checks": [], "errors": []})


def test_default_apply_transaction_timeout_during_edit_issue_txn_yields_unknown_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-6 items 1+2: a `subprocess.TimeoutExpired` raised by the REAL
    `subprocess.run()` call that dispatches edit_issue_txn.py (inside the
    default apply_transaction, not through the injectable seam) must
    resolve to `mutation_outcome=unknown` and trigger the AC5 authoritative-
    readback path -- never crash uncaught, never degrade to
    `failed_no_mutation`."""
    result_path = _write_candidate(tmp_path)
    readiness_stdout = _readiness_stdout_ok()

    def _fake_run(argv, **kwargs):
        script_path = str(argv[1])
        if "edit_issue_txn.py" in script_path:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, stdout=readiness_stdout, stderr="")

    monkeypatch.setattr(rrp.subprocess, "run", _fake_run)

    fetch_calls: list[None] = []

    def _fetch():
        fetch_calls.append(None)
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == "unknown"
    assert result["mutation_outcome"] != "not_attempted"
    assert result["phase"] != "complete"
    assert result["receipt"]["executor_status"] == "mutation_outcome_unknown"
    assert result["receipt"]["failure_code"] == "final_readback_unresolvable"
    # AC5: the authoritative readback must have actually run -- fetch() was
    # called at least once for the precondition read and again to resolve
    # the unknown outcome.
    assert len(fetch_calls) >= 2
    assert result["receipt"]["final_readback"]["status"] == "verified"


def test_default_apply_transaction_oserror_during_edit_issue_txn_yields_unknown_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-6 items 1+2: an `OSError` raised by the REAL `subprocess.run()`
    call (e.g. a transient OS-level failure) must resolve the SAME way as a
    TimeoutExpired -- `unknown`, never an uncaught crash, never
    `failed_no_mutation`."""
    result_path = _write_candidate(tmp_path)
    readiness_stdout = _readiness_stdout_ok()

    def _fake_run(argv, **kwargs):
        script_path = str(argv[1])
        if "edit_issue_txn.py" in script_path:
            raise OSError("transient os-level subprocess failure")
        return subprocess.CompletedProcess(argv, 0, stdout=readiness_stdout, stderr="")

    monkeypatch.setattr(rrp.subprocess, "run", _fake_run)

    def _fetch():
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == "unknown"
    assert result["mutation_outcome"] != "not_attempted"
    assert result["receipt"]["executor_status"] == "mutation_outcome_unknown"


def test_default_apply_transaction_truncated_non_json_stdout_yields_unknown_not_failed_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-6 item 3: when the REAL edit_issue_txn.py subprocess call
    completes (no exception) but its stdout is empty/truncated/non-JSON,
    the outcome must be `unknown` -- NEVER the `failed_no_mutation`
    shortcut this previously used, since a PATCH may genuinely have been
    dispatched even though stdout confirmation could not be read."""
    result_path = _write_candidate(tmp_path)
    readiness_stdout = _readiness_stdout_ok()

    def _fake_run(argv, **kwargs):
        script_path = str(argv[1])
        if "edit_issue_txn.py" in script_path:
            # Non-zero return code AND truncated/non-JSON stdout -- exactly
            # the "may have run and dispatched a mutation, but we lost the
            # confirmation" shape the review flagged as unsafe to degrade.
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="connection reset while reading child stdout"
            )
        return subprocess.CompletedProcess(argv, 0, stdout=readiness_stdout, stderr="")

    monkeypatch.setattr(rrp.subprocess, "run", _fake_run)

    def _fetch():
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == "unknown"
    assert result["mutation_outcome"] != "not_attempted"
    assert result["receipt"]["executor_status"] == "mutation_outcome_unknown"
    assert result["receipt"]["failure_code"] == "final_readback_unresolvable"


def test_default_apply_transaction_unknown_dispatch_paths_mark_patch_attempted_for_fresh_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-6 / AC9 consistency: a subprocess-level unknown outcome detected
    HERE (timeout/OSError/non-JSON stdout) reflects an actual attempt to
    dispatch edit_issue_txn.py, so it must set `receipt.patch_attempted`
    True (mirroring a genuine executor-reported `mutation_outcome_unknown`
    receipt) so AC9 fresh validation still runs afterward, instead of
    silently staying `not_run`."""
    result_path = _write_candidate(tmp_path)
    readiness_stdout = _readiness_stdout_ok()

    def _fake_run(argv, **kwargs):
        script_path = str(argv[1])
        if "edit_issue_txn.py" in script_path:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, stdout=readiness_stdout, stderr="")

    monkeypatch.setattr(rrp.subprocess, "run", _fake_run)

    fresh_validate_calls: list[str] = []

    def _fresh_validate(body: str) -> dict:
        fresh_validate_calls.append(body)
        return {"actionable_repair": False, "source_lane": "unanchored", "error": None}

    def _fetch():
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        fresh_validate=_fresh_validate,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["receipt"]["patch_attempted"] is True
    assert result["fresh_validation"]["status"] != "not_run"
    assert fresh_validate_calls, "fresh_validate producer must actually run after an unknown dispatch outcome"


def test_repair_action_apply_outer_registry_timeout_covers_worst_case_inner_budget_plus_reserve() -> None:
    """P0-6: a simple static arithmetic assertion (never a live-timing
    test) that the `repair_action.apply` registry entry's outer supervisor
    `timeout_seconds` stays strictly greater than the documented worst-case
    inner critical path (readiness subprocess + edit_issue_txn.py
    subprocess) plus the AC5 readback reserve -- this is exactly the
    timeout-budget mismatch the review found (a 60s outer timeout against a
    90s worst-case inner critical path)."""
    import command_registry

    entry = command_registry.REGISTRY["repair_action.apply"]
    inner_total = (
        rrp.REPAIR_APPLY_READINESS_SUBPROCESS_TIMEOUT_SECONDS
        + rrp.REPAIR_APPLY_EDIT_ISSUE_TXN_SUBPROCESS_TIMEOUT_SECONDS
    )
    required_minimum = inner_total + rrp.REPAIR_APPLY_READBACK_RESERVE_SECONDS

    assert inner_total == 90, "readiness(30s) + edit_issue_txn.py(60s) worst-case critical path"
    assert entry["timeout_seconds"] > required_minimum, (
        f"outer timeout_seconds={entry['timeout_seconds']} must exceed "
        f"inner_total({inner_total}) + readback_reserve"
        f"({rrp.REPAIR_APPLY_READBACK_RESERVE_SECONDS}) = {required_minimum}"
    )
    assert entry["timeout_seconds"] >= required_minimum + rrp.REPAIR_APPLY_OUTER_TIMEOUT_MARGIN_SECONDS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
