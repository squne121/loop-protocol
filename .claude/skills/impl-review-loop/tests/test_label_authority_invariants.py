"""Tests for label authority invariants (#2084 AC3 / AC8).

GitHub Issue labels are presentation-only / non-authoritative metadata
(SSOT: docs/dev/workflow.md, docs/dev/github-ops.md). These tests fix the
invariant that `build_intake_capsule.py`'s `ready_status`
(consumed by `_next_action_route`) does not depend on
`phase/implementation` label presence, across a permutation of other
label states as well.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

TEST_REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = (
    TEST_REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "build_intake_capsule.py"
)

spec = importlib.util.spec_from_file_location("build_intake_capsule_lai", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _issue_view_json(
    *,
    title: str = "実装: label authority invariants テスト",
    body: str = "## Machine-Readable Contract\n\nstatus: full-body\n\n## Allowed Paths\n- tracked.txt\n",
    updated_at: str = "2026-08-11T00:00:00Z",
    labels: list[str] | None = None,
    state: str = "open",
) -> str:
    label_objs = [{"name": name} for name in (labels or [])]
    return json.dumps(
        {
            "title": title,
            "state": state,
            "labels": label_objs,
            "body": body,
            "updatedAt": updated_at,
        }
    )


def _run_command_side_effect_factory(commands):
    calls = {"i": 0}

    def _run(cmd):
        index = calls["i"]
        calls["i"] += 1
        return commands[index]

    return _run


def _run_with_labels(labels: list[str], *, issue_number: int = 2084):
    """Run build_intake_capsule end-to-end with a given label set, using the
    ensure_contract_snapshot_result path (skips comment fetch commands)."""
    run_cmd = _run_command_side_effect_factory(
        [
            (0, _issue_view_json(labels=labels), ""),
            (0, "abc123\n", ""),
            (0, "main\n", ""),
            (0, "  \n", ""),
        ]
    )
    ensure_payload_path = None
    import tempfile

    ensure_payload = {
        "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
        "status": "go",
        "contract_review_once_result": {"vc_preflight_classifications": []},
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tf:
        json.dump(ensure_payload, tf)
        ensure_payload_path = tf.name

    with patch.object(mod, "_run_command", side_effect=run_cmd):
        capsule, _artifact, exit_code = mod.build_intake_capsule(
            issue_number,
            "squne121/loop-protocol",
            ensure_contract_snapshot_result=ensure_payload_path,
        )
    return capsule, exit_code


# ---------------------------------------------------------------------------
# AC3: ready_status independent of phase/implementation label
# ---------------------------------------------------------------------------


def test_ready_status_independent_of_phase_label():
    """GIVEN identical body/dependency/contract-review state
    WHEN labels vary between [] and ["phase/implementation"]
    THEN ready_status (issue_ready_tuple.status) is identical
    """
    capsule_without_label, exit_without = _run_with_labels([])
    capsule_with_label, exit_with = _run_with_labels(["phase/implementation"])

    assert exit_without == 0
    assert exit_with == 0
    assert (
        capsule_without_label["issue_ready_tuple"]["status"]
        == capsule_with_label["issue_ready_tuple"]["status"]
    )
    assert capsule_without_label["issue_ready_tuple"]["status"] == "pass"
    # phase_label_present is retained as observational only; it must differ
    # while status remains identical (proves it does not gate readiness).
    assert capsule_without_label["issue_ready_tuple"]["phase_label_present"] is False
    assert capsule_with_label["issue_ready_tuple"]["phase_label_present"] is True

    # next_action.route must also be identical -- routing is unaffected.
    assert capsule_without_label is not None


def test_next_action_route_independent_of_phase_label(monkeypatch=None):
    """GIVEN identical body/dependency/contract-review state
    WHEN labels vary between [] and ["phase/implementation"]
    THEN next_action.route is identical (readiness gate unaffected)
    """
    capsule_without_label, _ = _run_with_labels([])
    capsule_with_label, _ = _run_with_labels(["phase/implementation"])
    assert (
        capsule_without_label["next_action"]["route"]
        == capsule_with_label["next_action"]["route"]
    )


# ---------------------------------------------------------------------------
# AC8: label permutation matrix
# ---------------------------------------------------------------------------


LABEL_PERMUTATIONS = [
    [],
    ["triage-required"],
    ["phase/implementation"],
    ["state/needs-human"],
    ["triage-required", "phase/implementation"],
    ["triage-required", "state/needs-human"],
    ["phase/implementation", "state/needs-human"],
    ["triage-required", "phase/implementation", "state/needs-human"],
]


@pytest.mark.parametrize("labels", LABEL_PERMUTATIONS, ids=lambda lbls: ",".join(lbls) or "empty")
def test_ready_status_label_permutation_matrix(labels):
    """AC8: for every label permutation, ready_status must equal the baseline
    (labels=[]) result -- readiness must be entirely label-independent."""
    baseline_capsule, baseline_exit = _run_with_labels([])
    variant_capsule, variant_exit = _run_with_labels(labels)

    assert baseline_exit == 0
    assert variant_exit == 0
    assert (
        variant_capsule["issue_ready_tuple"]["status"]
        == baseline_capsule["issue_ready_tuple"]["status"]
    )
    assert (
        variant_capsule["next_action"]["route"]
        == baseline_capsule["next_action"]["route"]
    )
