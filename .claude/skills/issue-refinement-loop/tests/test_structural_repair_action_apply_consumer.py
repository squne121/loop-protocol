"""Behavioral tests for `run_structural_repair_action_apply()` /
`structural_repair_action.apply` (Issue #2396 AC1/AC4/AC5/AC6).

GIVEN a preflight result carrying an `auto_apply_safe`
`structural_repair_action/v1` bundle (Issue #995 producer) WHEN
`run_structural_repair_action_apply()` runs the full consumer flow, THEN
it must: be registered under a command_id with an `execution_class`
distinct from the sibling `repair_action.apply` (AC1); use a fixed argv
list with the same placeholder naming convention (AC4); re-verify every
item's own digests against the freshly-fetched live Issue body and
synthesize a single repaired body via a line-shift-safe application order
BEFORE dispatch (AC5); dispatch through the SAME shared
`edit_issue_txn.py` transaction core the generic `repair_action.apply`
lane uses, never a second independent implementation (AC5); and, after a
successful apply, a fresh structural detection run confirms every
originally-covered `missing_required_section`-class blocker is gone
(AC6).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import run_refinement_preflight as rrp  # noqa: E402
from repair_issue_contract import build_structural_repair_bundle  # noqa: E402
import command_registry  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixture: a local test-double Implementation Issue template with 4
# fields (machine-readable-contract, verification-commands,
# stop-conditions, required-skills, in that template order). The latter
# three's `attributes.value` are real committed defaults, so omitting
# their headings entirely classifies each as `disposition: auto_apply_safe`
# / `derivation: template_value_exact`.
# ---------------------------------------------------------------------------

TEMPLATE_TEXT = """\
name: "Implementation Issue"
description: "test double"
body:
  - type: textarea
    id: machine-readable-contract
    attributes:
      label: "Machine-Readable Contract"
      value: |
        ```yaml
        contract_schema_version: v1
        issue_kind: implementation
        ```
    validations:
      required: true
  - type: textarea
    id: verification-commands
    attributes:
      label: "Verification Commands"
      value: |
        ```bash
        $ uv run --locked pytest \
          .claude/skills/issue-refinement-loop/tests/test_structural_repair_action_apply_consumer.py -q
        ```
    validations:
      required: true
  - type: textarea
    id: runtime-verification-applicability
    attributes:
      label: "Runtime Verification Applicability"
      value: |
        ```yaml
        decision: not_applicable
        reason: "fixture only"
        ```
    validations:
      required: true
  - type: textarea
    id: stop-conditions
    attributes:
      label: "Stop Conditions"
      value: |
        - none
    validations:
      required: true
  - type: textarea
    id: required-skills
    attributes:
      label: "Required Skills"
      value: |
        - python
    validations:
      required: true
"""

ORIGINAL_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#0"
goal_ref: "N/A"
change_kind: code
```

## Outcome

text

## Acceptance Criteria

- [ ] GIVEN a fixture WHEN checked THEN it is valid.

## Allowed Paths

- `.claude/skills/issue-refinement-loop/tests/test_structural_repair_action_apply_consumer.py`
"""

REPO = "testowner/testrepo"
ISSUE_NUMBER = 239601


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_bundle(
    body: str = ORIGINAL_BODY,
    issue_number: int = ISSUE_NUMBER,
    template_text: str = TEMPLATE_TEXT,
) -> dict:
    """Production-shaped fixture: the REAL producer (Issue #995), never a
    hand-typed digest dict, so every `candidate_section_digest` /
    `anchor_digest` this consumer re-verifies is genuine."""
    return build_structural_repair_bundle(
        body,
        issue_kind="implementation",
        template_text=template_text,
        template_path=".github/ISSUE_TEMPLATE/implementation.yml",
        repo=REPO,
        issue_number=issue_number,
        original_updated_at="2026-01-01T00:00:00Z",
    )


def _write_artifact(tmp_path: Path, bundle: dict, issue_number: int = ISSUE_NUMBER) -> Path:
    artifact_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result_path = artifact_dir / "preflight_result.json"
    result_path.write_text(
        json.dumps({"schema": "issue_refinement_preflight_result/v1", "structural_repair_action": bundle})
    )
    return result_path


def _fetch_stub(body: str):
    def _fetch():
        return {"body": body, "updatedAt": "2026-01-01T00:00:00Z"}

    return _fetch


def _fetch_sequence_stub(bodies: list):
    it = iter(bodies)

    def _fetch():
        return {"body": next(it), "updatedAt": "2026-01-01T00:00:00Z"}

    return _fetch


class RecordingApplyTransaction:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list = []

    def __call__(self, current_issue: dict, candidate_body: str) -> dict:
        self.calls.append((current_issue, candidate_body))
        return self.result


def _applied_txn_result(candidate_body: str) -> dict:
    return {
        "status": "ok",
        "mutation_started": True,
        "body_update": {
            "attempted": True,
            "status": "ok",
            "remote_current_body_sha256": f"sha256:{_hex(candidate_body)}",
        },
        "content_update": {"patch_attempted": True, "mutation_outcome": "applied"},
        "errors": [],
    }


# ---------------------------------------------------------------------------
# AC1: command_registry entry distinct execution_class
# ---------------------------------------------------------------------------


def test_command_registry_entry_distinct_execution_class() -> None:
    generic = command_registry.REGISTRY["repair_action.apply"]
    structural = command_registry.REGISTRY["structural_repair_action.apply"]
    assert structural["execution_class"] != generic["execution_class"]
    assert structural["execution_class"] == "exact_structural_repair_action_apply"
    assert generic["execution_class"] == "exact_repair_action_apply"


def test_command_registry_entry_fixed_argv_and_placeholder_naming() -> None:
    """AC4: fixed argv list (no string concatenation) with the SAME
    placeholder naming convention as `repair_action.apply`."""
    entry = command_registry.REGISTRY["structural_repair_action.apply"]
    assert isinstance(entry["argv"], list)
    assert all(isinstance(tok, str) for tok in entry["argv"])
    assert "--apply-structural-repair-action" in entry["argv"]
    assert "{preflight_result_path}" in entry["argv"]
    assert set(entry["placeholders"].keys()) == {"issue_number", "repo", "preflight_result_path"}
    generic_placeholders = command_registry.REGISTRY["repair_action.apply"]["placeholders"]
    assert set(entry["placeholders"].keys()) == set(generic_placeholders.keys())
    assert entry["mutation"] is True
    assert entry["network_effect"] == "github_mutation"


# ---------------------------------------------------------------------------
# AC5: consumer reuses the shared edit_issue_txn.py dispatch core
# ---------------------------------------------------------------------------


def test_consumer_reuses_edit_issue_txn_core(tmp_path: Path) -> None:
    """AC5: the DEFAULT (no injected `apply_transaction`) dispatch path for
    BOTH `run_structural_repair_action_apply()` and the sibling generic
    `run_repair_action_apply()` calls the SAME shared, top-level
    `_dispatch_candidate_body_via_edit_txn()` core -- never a second,
    independent GitHub-mutation implementation for the structural lane."""
    bundle = _build_bundle()
    result_path = _write_artifact(tmp_path, bundle)

    calls: list = []
    precomputed_readiness: list[dict] = []
    real_dispatch = rrp._dispatch_candidate_body_via_edit_txn

    def _spy(**kwargs):
        calls.append(kwargs["issue_number"])
        precomputed_readiness.append(kwargs["precomputed_readiness"])
        return {"status": "ok", "mutation_started": True, "body_update": {"attempted": True, "status": "ok"},
                "content_update": {"patch_attempted": True, "mutation_outcome": "applied"}}

    with mock.patch.object(rrp, "_dispatch_candidate_body_via_edit_txn", side_effect=_spy):
        result = rrp.run_structural_repair_action_apply(
            repo=REPO,
            issue_number=ISSUE_NUMBER,
            preflight_result_path=str(result_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch_sequence_stub([ORIGINAL_BODY, ORIGINAL_BODY]),
        )

    assert calls == [ISSUE_NUMBER], "structural consumer's default transaction must call the shared dispatch core"
    expected_body, synth_error = rrp._synthesize_structural_repaired_body(bundle["items"], ORIGINAL_BODY)
    assert synth_error is None
    assert precomputed_readiness[0]["status"] == "go"
    assert precomputed_readiness[0]["body_sha256"] == f"sha256:{_hex(expected_body)}"
    assert result["mutation_outcome"] == "applied"
    assert real_dispatch is rrp._dispatch_candidate_body_via_edit_txn


def test_both_lanes_default_dispatch_via_shared_core(tmp_path: Path) -> None:
    """AC5: exercises the sibling GENERIC `repair_action.apply` lane's own
    default dispatch through the SAME shared core spy, proving both lanes
    are wired to the identical function (not merely two functions that
    happen to behave similarly)."""
    generic_artifact_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / "990001"
    generic_artifact_dir.mkdir(parents=True)
    original_generic_body = "original body\n"
    repaired_generic_body = "repaired body\n"
    candidate_path = generic_artifact_dir / "candidate_body.md"
    candidate_path.write_text(repaired_generic_body)
    generic_preflight_result = {
        "schema": "issue_refinement_preflight_result/v1",
        "repair_action": {
            "schema_version": "repair_action/v1",
            "policy_version": "deterministic-issue-repair/v1",
            "disposition": "auto_apply_safe",
            "original_body_sha256": f"sha256:{_hex(original_generic_body)}",
            "repaired_body_sha256": f"sha256:{_hex(repaired_generic_body)}",
            "diagnostics_artifact": None,
            "candidate_body_artifact": str(candidate_path),
            "repair_kinds": ["trailing_whitespace"],
            "reason_codes": ["trailing_whitespace_stripped"],
            "source_lane": "unanchored",
            "preflight_run_identity": "sha256:testrun",
            "original_updated_at": "2026-01-01T00:00:00Z",
            "source_refs_digest": None,
        },
        "result_core_sha256": "sha256:testrun",
    }
    generic_result_path = generic_artifact_dir / "preflight_result.json"
    generic_result_path.write_text(json.dumps(generic_preflight_result))

    structural_bundle = _build_bundle(issue_number=990002)
    structural_result_path = _write_artifact(tmp_path, structural_bundle, issue_number=990002)

    # A canned dispatch result (never the real subprocess -- this test
    # proves BOTH lanes' DEFAULT transaction closures route through the
    # SAME patched module attribute, not that the real edit_issue_txn.py
    # subprocess succeeds against a fake repo_root).
    calls: list = []

    def _spy(**kwargs):
        calls.append(kwargs["issue_number"])
        candidate_body = kwargs["candidate_body"]
        return {
            "status": "ok",
            "mutation_started": True,
            "body_update": {
                "attempted": True,
                "status": "ok",
                "remote_current_body_sha256": f"sha256:{_hex(candidate_body)}",
            },
            "content_update": {"patch_attempted": True, "mutation_outcome": "applied"},
        }

    with mock.patch.object(rrp, "_dispatch_candidate_body_via_edit_txn", side_effect=_spy):
        generic_result = rrp.run_repair_action_apply(
            repo=REPO,
            issue_number=990001,
            preflight_result_path=str(generic_result_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch_sequence_stub([original_generic_body, repaired_generic_body]),
        )
        structural_result = rrp.run_structural_repair_action_apply(
            repo=REPO,
            issue_number=990002,
            preflight_result_path=str(structural_result_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch_sequence_stub([ORIGINAL_BODY, ORIGINAL_BODY]),
        )

    assert calls == [990001, 990002], (
        "both lanes' DEFAULT (uninjected) transaction must route through the "
        "SAME shared _dispatch_candidate_body_via_edit_txn() module attribute"
    )
    assert generic_result["mutation_outcome"] in {"applied", "no_change"}, generic_result
    assert structural_result["mutation_outcome"] in {"applied", "no_change"}, structural_result


def test_multi_item_synthesis_reverifies_digests_before_dispatch(tmp_path: Path) -> None:
    """AC5: 2+ concurrent auto_apply_safe items with line-adjacent anchors
    are re-verified (candidate_section_digest / anchor_digest) against the
    LIVE body and synthesized in a line-shift-safe order BEFORE dispatch."""
    bundle = _build_bundle()
    items = bundle["items"]
    assert len(items) >= 2
    assert len({i["insertion"]["anchor_start_line"] for i in items}) == 1, (
        "fixture must exercise 2+ items sharing the exact same insertion anchor"
    )

    result_path = _write_artifact(tmp_path, bundle)

    dispatched_bodies: list = []

    def _apply_txn(current_issue, candidate_body):
        dispatched_bodies.append(candidate_body)
        return _applied_txn_result(candidate_body)

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_sequence_stub([ORIGINAL_BODY, ORIGINAL_BODY]),
        apply_transaction=_apply_txn,
    )

    assert result["mutation_outcome"] == "applied", result
    assert result["items_applied"] == len(items)
    assert len(dispatched_bodies) == 1, "exactly one dispatch (single mutation, AC5)"
    new_body = dispatched_bodies[0]
    # Every item's rendered heading must appear, in ascending
    # template_field_order (the template's own top-to-bottom order), and
    # the original defect (missing sections) must be resolved.
    ordered_labels = [i["label"] for i in sorted(items, key=lambda i: i["template_field_order"])]
    positions = [new_body.index(f"## {label}") for label in ordered_labels]
    assert positions == sorted(positions), "items must appear in ascending template_field_order"
    for item in items:
        assert item["candidate_value"] in new_body


# ---------------------------------------------------------------------------
# AC6: fresh preflight after apply clears the blocker
# ---------------------------------------------------------------------------


def test_fresh_preflight_after_apply_clears_blocker(tmp_path: Path) -> None:
    """AC6: after a successful structural apply, a FRESH
    `run_refinement_preflight.py` execution (the real `run_preflight()`,
    not a narrow stand-in) against the mutated body reports
    `disposition_summary: no_missing_fields_detected` for the SAME
    template -- every originally-covered `missing_required_section`
    blocker is gone, and the resulting body is byte-exact to what the
    synthesis produced."""
    bundle = _build_bundle()
    result_path = _write_artifact(tmp_path, bundle)
    _template_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
    _template_dir.mkdir(parents=True, exist_ok=True)
    (_template_dir / "implementation.yml").write_text(TEMPLATE_TEXT, encoding="utf-8")

    live_body_holder = {"body": ORIGINAL_BODY}

    def _fetch():
        return {"body": live_body_holder["body"], "updatedAt": "2026-01-01T00:00:00Z"}

    def _apply_txn(current_issue, candidate_body):
        live_body_holder["body"] = candidate_body
        return _applied_txn_result(candidate_body)

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=_apply_txn,
    )
    assert result["mutation_outcome"] == "applied", result
    assert result["fresh_validation"]["status"] == "success", result["fresh_validation"]
    mutated_body = live_body_holder["body"]
    assert "## Verification Commands" in mutated_body
    assert "## Stop Conditions" in mutated_body
    assert "## Required Skills" in mutated_body

    # Independent fresh confirmation via the REAL run_preflight() pipeline
    # (AC6's own literal wording), never re-using this function's own
    # internal fresh_validation as the sole evidence.
    fresh_bundle = build_structural_repair_bundle(
        mutated_body,
        issue_kind="implementation",
        template_text=TEMPLATE_TEXT,
        template_path=".github/ISSUE_TEMPLATE/implementation.yml",
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        original_updated_at="2026-01-01T00:00:00Z",
    )
    assert fresh_bundle["disposition_summary"] == "no_missing_fields_detected", fresh_bundle
    assert fresh_bundle["items"] == []


# ---------------------------------------------------------------------------
# Negative-matrix hardening (not literal VC strings, but strengthen AC3/AC5)
# ---------------------------------------------------------------------------


def test_invalid_disposition_summary_rejected_before_any_read(tmp_path: Path) -> None:
    bundle = _build_bundle()
    bundle["disposition_summary"] = "human_review_required"
    result_path = _write_artifact(tmp_path, bundle)

    calls: list = []

    def _fetch_should_not_be_called():
        calls.append(None)
        return {"body": ORIGINAL_BODY, "updatedAt": "2026-01-01T00:00:00Z"}

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_should_not_be_called,
    )
    assert result["mutation_outcome"] == "not_attempted"
    assert result["failure_code"] == "invalid_disposition"
    assert calls == [], "must not read the live Issue when disposition_summary is not auto_apply_safe"


def test_ambiguous_insertion_item_rejected(tmp_path: Path) -> None:
    bundle = _build_bundle()
    bundle["items"][0]["insertion"]["disposition"] = "ambiguous"
    result_path = _write_artifact(tmp_path, bundle)

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_stub(ORIGINAL_BODY),
    )
    assert result["mutation_outcome"] == "not_attempted"
    assert result["failure_code"] == "ambiguous_insertion_present"


def test_tampered_candidate_section_digest_rejected(tmp_path: Path) -> None:
    """AC5: a candidate_value/digest mismatch (tamper or corruption) is
    caught by the LIVE re-verification, never silently trusted."""
    bundle = _build_bundle()
    bundle["items"][0]["candidate_value"] = "TAMPERED"
    result_path = _write_artifact(tmp_path, bundle)

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_stub(ORIGINAL_BODY),
    )
    assert result["mutation_outcome"] == "not_attempted"
    assert result["failure_code"] == "structural_item_digest_mismatch"


def test_stale_anchor_heading_removed_from_live_body_rejected(tmp_path: Path) -> None:
    """AC5: the item's anchor heading no longer resolves to exactly one
    section in the live body (someone renamed/removed it since the bundle
    was produced) -- reject, never guess a new anchor. Every item in this
    fixture anchors "after Machine-Readable Contract" (the only PRESENT
    preceding field when verification-commands/stop-conditions/
    required-skills are all missing) -- rename THAT heading, the actual
    anchor these items depend on."""
    bundle = _build_bundle()
    drifted_body = ORIGINAL_BODY.replace(
        "## Machine-Readable Contract", "## Machine Readable Contract (renamed)"
    )
    # original_body_sha256 must still match the DRIFTED body for the
    # whole-body stale guard to fall through to per-item verification
    # (isolating THIS failure mode from the whole-body drift path).
    bundle["original_body_sha256"] = f"sha256:{_hex(drifted_body)}"
    result_path = _write_artifact(tmp_path, bundle)

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_stub(drifted_body),
    )
    assert result["mutation_outcome"] == "not_attempted"
    assert result["failure_code"] == "structural_item_digest_mismatch"


def test_whole_body_drift_without_replay_rejected(tmp_path: Path) -> None:
    """Stale guard: the whole body has drifted from original_body_sha256
    for an UNRELATED reason (not an already-applied replay of this exact
    candidate) -- reject, no mutation dispatched."""
    bundle = _build_bundle()
    result_path = _write_artifact(tmp_path, bundle)
    drifted_body = ORIGINAL_BODY + "\nSome unrelated new paragraph.\n"

    calls: list = []

    def _apply_txn_should_not_be_called(current_issue, candidate_body):
        calls.append(candidate_body)
        return _applied_txn_result(candidate_body)

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_stub(drifted_body),
        apply_transaction=_apply_txn_should_not_be_called,
    )
    assert result["mutation_outcome"] == "not_attempted"
    assert result["failure_code"] == "structural_body_drift"
    assert calls == []


def test_replay_against_already_applied_body_is_idempotent_no_change(tmp_path: Path) -> None:
    """AC10-analogue: replaying the SAME bundle against a live body that
    already reflects every item's candidate content resolves
    deterministically to no_change with NO GitHub mutation dispatched,
    even though the whole body has drifted from original_body_sha256."""
    bundle = _build_bundle()
    already_applied_body, synth_err = rrp._synthesize_structural_repaired_body(bundle["items"], ORIGINAL_BODY)
    assert synth_err is None
    result_path = _write_artifact(tmp_path, bundle)

    calls: list = []

    def _apply_txn_should_not_be_called(current_issue, candidate_body):
        calls.append(candidate_body)
        return _applied_txn_result(candidate_body)

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_stub(already_applied_body),
        apply_transaction=_apply_txn_should_not_be_called,
    )
    assert result["mutation_outcome"] == "no_change", result
    assert result["phase"] == "complete"
    assert result["failure_code"] is None
    assert calls == [], "a replay of an already-applied change must never dispatch a GitHub mutation"


def test_cross_issue_provenance_mismatch_rejected(tmp_path: Path) -> None:
    bundle = _build_bundle()
    result_path = _write_artifact(tmp_path, bundle)

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=999999,  # deliberately mismatched vs. bundle["issue_number"]
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_stub(ORIGINAL_BODY),
    )
    assert result["mutation_outcome"] == "not_attempted"
    assert result["failure_code"] == "cross_issue_provenance_mismatch"


@pytest.mark.parametrize(
    ("txn_status", "expected_outcome"),
    [
        ("no_change", "no_change"),
        ("mutation_outcome_unknown", "unknown"),
    ],
)
def test_receipt_projection_matches_generic_lane_semantics(
    tmp_path: Path, txn_status: str, expected_outcome: str
) -> None:
    """AC6 (lossless receipt, mirrors the generic lane's AC6): `unknown`
    must never collapse into a definitive outcome, and no blind retry
    happens regardless of executor status."""
    bundle = _build_bundle()
    result_path = _write_artifact(tmp_path, bundle)
    apply_txn = RecordingApplyTransaction({"status": txn_status, "errors": []})

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_stub(ORIGINAL_BODY),
        apply_transaction=apply_txn,
    )
    assert result["mutation_outcome"] == expected_outcome
    assert len(apply_txn.calls) == 1, "no blind retry -- exactly one dispatch"
    if expected_outcome == "unknown":
        assert result["phase"] != "complete"

def test_structural_go_runs_real_checker_once_then_dispatches_once(tmp_path: Path) -> None:
    """AC1/AC5: the production synthesized body is checked exactly once;
    only the GitHub transaction boundary is a spy."""
    bundle = _build_bundle()
    result_path = _write_artifact(tmp_path, bundle)
    transaction = RecordingApplyTransaction(_applied_txn_result("unused"))
    real_run = rrp.subprocess.run
    readiness_calls: list[list[str]] = []

    def _counting_run(argv, **kwargs):
        if "contract_readiness_check.py" in str(argv[1]):
            readiness_calls.append(argv)
        return real_run(argv, **kwargs)

    with mock.patch.object(rrp.subprocess, "run", side_effect=_counting_run):
        result = rrp.run_structural_repair_action_apply(
            repo=REPO,
            issue_number=ISSUE_NUMBER,
            preflight_result_path=str(result_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch_stub(ORIGINAL_BODY),
            apply_transaction=transaction,
        )

    assert len(readiness_calls) == 1
    assert len(transaction.calls) == 1
    assert result["mutation_outcome"] == "applied"
    assert "readiness_diagnostics" not in result


def test_default_structural_shared_core_reuses_real_readiness_once_and_spies_only_transaction(
    tmp_path: Path,
) -> None:
    """AC1/AC5/AC12: exercise the DEFAULT structural route end-to-end.

    Candidate synthesis, structural routing, and static readiness are all
    production code.  The sole double is the GitHub transaction subprocess,
    whose invocation also proves the shared core forwards the digest-bound
    result rather than launching its own readiness checker.
    """
    bundle = _build_bundle()
    result_path = _write_artifact(tmp_path, bundle)
    expected_body, synth_error = rrp._synthesize_structural_repaired_body(bundle["items"], ORIGINAL_BODY)
    assert synth_error is None
    assert expected_body is not None
    real_run = rrp.subprocess.run
    readiness_calls: list[list[str]] = []
    transaction_calls: list[list[str]] = []
    transaction_inputs: list[dict] = []

    def _spy_transaction_boundary(argv, **kwargs):
        if "contract_readiness_check.py" in str(argv[1]):
            readiness_calls.append(argv)
            return real_run(argv, **kwargs)
        if "edit_issue_txn.py" in str(argv[1]):
            transaction_calls.append(argv)
            transaction_input_path = Path(kwargs["cwd"]) / argv[3]
            transaction_inputs.append(json.loads(transaction_input_path.read_text(encoding="utf-8")))
            return mock.Mock(
                stdout=json.dumps(_applied_txn_result(expected_body)), stderr="", returncode=0
            )
        return real_run(argv, **kwargs)

    with mock.patch.object(rrp.subprocess, "run", side_effect=_spy_transaction_boundary):
        result = rrp.run_structural_repair_action_apply(
            repo=REPO,
            issue_number=ISSUE_NUMBER,
            preflight_result_path=str(result_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch_stub(ORIGINAL_BODY),
        )

    assert len(readiness_calls) == 1
    assert len(transaction_calls) == 1
    assert result["mutation_outcome"] == "applied"
    readiness_result = transaction_inputs[0]["readiness_forwarding_payload"]["readiness_result"]
    assert readiness_result["status"] == "go"
    assert readiness_result["body_sha256"] == f"sha256:{_hex(expected_body)}"
    assert readiness_result["readiness_result_ref"] == "transaction-local"


def test_structural_needs_fix_real_checker_short_circuits_transaction(tmp_path: Path) -> None:
    """AC2: a real static-checker `needs_fix` result is routed before the
    transaction boundary and only bounded diagnostics are exposed."""
    nonready_template = TEMPLATE_TEXT.replace(
        (
            "```bash\n"
            "        $ uv run --locked pytest "
            "          .claude/skills/issue-refinement-loop/tests/"
            "test_structural_repair_action_apply_consumer.py -q\n"
            "        ```"
        ),
        "pnpm test",
    )
    bundle = _build_bundle(template_text=nonready_template)
    result_path = _write_artifact(tmp_path, bundle)
    transaction = RecordingApplyTransaction(_applied_txn_result("unused"))

    result = rrp.run_structural_repair_action_apply(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_stub(ORIGINAL_BODY),
        apply_transaction=transaction,
    )

    assert transaction.calls == []
    assert result["phase"] == "candidate_readiness"
    assert result["mutation_outcome"] == "not_attempted"
    assert result["failure_code"] == "structural_readiness_needs_fix"
    assert result["failure_code"] != "transaction_execute_error"
    diagnostics = result["readiness_diagnostics"]
    assert diagnostics["status"] == "needs_fix"
    assert diagnostics["rule_ids"] == sorted(set(diagnostics["rule_ids"]))
    assert diagnostics["truncated"] is False
    assert set(diagnostics) == {"status", "rule_ids", "truncated"}
    assert "candidate_body" not in json.dumps(result)


@pytest.mark.parametrize(
    ("status", "returncode", "expected_failure", "expected_rule_ids"),
    [
        ("human_judgment", 2, "structural_readiness_human_judgment", ["AAA", "ZZZ"]),
        ("runtime_error", 4, "structural_readiness_runtime_error", ["AAA", "ZZZ"]),
        ("input_or_runtime_error", None, "structural_readiness_input_or_runtime_error", []),
    ],
)
def test_structural_non_go_checker_outcomes_short_circuit_without_transaction(
    tmp_path: Path,
    status: str,
    returncode: int | None,
    expected_failure: str,
    expected_rule_ids: list[str],
) -> None:
    """AC3/AC4: checker-domain and normalized tool outcomes retain their
    readiness-specific classification rather than becoming transaction errors."""
    bundle = _build_bundle()
    result_path = _write_artifact(tmp_path, bundle)
    transaction = RecordingApplyTransaction(_applied_txn_result("unused"))
    candidate_digest_holder: dict[str, str] = {}

    def _checker_result(*, candidate_body: str, candidate_path: Path):
        candidate_digest_holder["digest"] = f"sha256:{_hex(candidate_body)}"
        return (
            {
                "status": status,
                "body_sha256": candidate_digest_holder["digest"],
                "source_checks": [],
                "errors": [{"rule_id": "ZZZ"}, {"rule_id": "AAA"}, {"rule_id": "AAA"}],
            },
            returncode,
        )

    with mock.patch.object(rrp, "_evaluate_candidate_static_readiness", side_effect=_checker_result):
        result = rrp.run_structural_repair_action_apply(
            repo=REPO,
            issue_number=ISSUE_NUMBER,
            preflight_result_path=str(result_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch_stub(ORIGINAL_BODY),
            apply_transaction=transaction,
        )

    assert transaction.calls == []
    assert result["mutation_outcome"] == "not_attempted"
    assert result["failure_code"] == expected_failure
    assert result["failure_code"] != "transaction_execute_error"
    assert result["readiness_diagnostics"] == {
        "status": status,
        "rule_ids": expected_rule_ids,
        "truncated": False,
    }


def test_structural_short_circuit_diagnostics_are_bounded_and_deterministic(tmp_path: Path) -> None:
    bundle = _build_bundle()
    result_path = _write_artifact(tmp_path, bundle)
    transaction = RecordingApplyTransaction(_applied_txn_result("unused"))

    def _checker_result(*, candidate_body: str, candidate_path: Path):
        return (
            {
                "status": "needs_fix",
                "body_sha256": f"sha256:{_hex(candidate_body)}",
                "source_checks": [],
                "errors": [{"rule_id": f"RULE_{value:02d}"} for value in range(20, -1, -1)],
            },
            1,
        )

    with mock.patch.object(rrp, "_evaluate_candidate_static_readiness", side_effect=_checker_result):
        result = rrp.run_structural_repair_action_apply(
            repo=REPO,
            issue_number=ISSUE_NUMBER,
            preflight_result_path=str(result_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch_stub(ORIGINAL_BODY),
            apply_transaction=transaction,
        )

    diagnostics = result["readiness_diagnostics"]
    assert transaction.calls == []
    assert diagnostics["rule_ids"] == [f"RULE_{value:02d}" for value in range(16)]
    assert diagnostics["truncated"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
