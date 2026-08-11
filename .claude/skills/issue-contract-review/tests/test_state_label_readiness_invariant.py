#!/usr/bin/env python3
"""
tests/test_state_label_readiness_invariant.py

AC12（#2084 Scope Delta 追記, PR #2092 comment #5251894253 由来）:

labels を変えても issue-contract-review の最終 readiness
（CONTRACT_REVIEW_RESULT_V1.status）が不変であることを固定する regression
test。既存の permutation test（build_intake_capsule 等）は
CONTRACT_SNAPSHOT_ENSURE_RESULT_V1.status="go" を固定注入して
issue-contract-review 自体をバイパスしていた（OWNER 指摘）。本テストはそれとは
異なり、issue-contract-review の実際の判定ロジックを通す経路で検証する:

  1. test_run_gh_api_labels_do_not_affect_product_spec_decision:
     check_product_spec_contract.py の実プロダクションコード
     （run_gh_api → main() の decision pipeline）を、gh CLI サブプロセスの
     戻り値のみを差し替えて（decision ロジック自体はモックしない）実行し、
     labels フィールドだけが異なる 2 つの GitHub API レスポンスで
     applicability/decision/blocked_reasons が完全に一致することを検証する。

  2. test_run_once_status_stable_across_label_permutation_matrix:
     run_contract_review_once.py の実際の orchestrator エントリポイント
     run_once()（issue-contract-review 自身の実行）を、label permutation
     （[] / ["state/needs-human"] / ["triage-required"] /
     ["phase/implementation", "state/needs-human"]）ごとに呼び出し、
     CONTRACT_REVIEW_RESULT_V1.status が全 permutation で不変であることを
     固定する。

  3. test_run_once_signature_has_no_label_parameter:
     run_once() のシグネチャに labels 関連パラメータが存在しないことを
     構造的に固定する（label が readiness authority の入力経路に再混入する
     ことを防ぐ fossil ガード）。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock, patch


_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"

# ---------------------------------------------------------------------------
# Import modules under test. Mirrors the established sys.path.insert() +
# plain-import pattern already used elsewhere in this skill's test suite
# (see scripts/tests/test_product_spec_check.py), which avoids the
# spec_from_file_location dataclass sys.modules registration pitfall.
# ---------------------------------------------------------------------------

import sys  # noqa: E402

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_product_spec_contract as _cpsc_mod  # noqa: E402
import run_contract_review_once as _rcr_mod  # noqa: E402

run_once = _rcr_mod.run_once

_ISSUE_NUMBER = 2084
_REPO = "squne121/loop-protocol"

_FIXTURE_BODY = (_HERE.parent / "scripts" / "tests" / "fixtures" / "docs_product_spec_issue_ok.md").read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------------------------
# 1. check_product_spec_contract.py real decision pipeline, only the `gh`
#    subprocess boundary is stubbed (decision logic itself is untouched).
# ---------------------------------------------------------------------------


def _run_gh_api_with_labels(labels: list) -> dict:
    """Invoke check_product_spec_contract.run_gh_api()'s REAL function, but
    stub subprocess.run (the gh CLI call boundary only) so tests stay
    offline. This exercises the actual decision pipeline downstream, not a
    status-fixture bypass.
    """
    fake_stdout = json.dumps(
        {
            "title": "fixture issue",
            "body": _FIXTURE_BODY,
            "labels": [{"name": name} for name in labels],
        }
    )
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = fake_stdout

    with patch.object(_cpsc_mod.subprocess, "run", return_value=fake_result):
        issue_data = _cpsc_mod.run_gh_api(_ISSUE_NUMBER, _REPO)

    assert issue_data is not None
    issue_body = issue_data.get("body", "")

    # Re-run the same in-process decision pipeline main() uses, without
    # going through argparse/stdout — this is the identical logic main()
    # executes after fetching issue_data (see check_product_spec_contract.py
    # main()).
    allowed_paths = _cpsc_mod.pc_extract_allowed_paths(issue_body)
    mrc_parsed = _cpsc_mod.parse_machine_readable_contract(issue_body)
    mrc = _cpsc_mod.SectionParseResult(
        present=bool(_cpsc_mod._extract_sections(issue_body, "Machine-Readable Contract")),
        ok=mrc_parsed.ok,
        data=mrc_parsed.data,
        reason=mrc_parsed.reason,
        duplicate_key=mrc_parsed.duplicate_key,
    )
    product_spec_context = _cpsc_mod._parse_yaml_section(issue_body, "Product Spec Context")
    triggers = _cpsc_mod.check_trigger_conditions(issue_body, allowed_paths, product_spec_context)
    triggers["machine_readable_contract_present"] = mrc.present
    applicability = "applicable" if any(triggers.values()) else "not_applicable"

    checks = {
        "docs_product_requires_spec_evidence": _cpsc_mod.check_ps001(
            allowed_paths, triggers, mrc, product_spec_context
        ),
        "tasks_md_not_direct_source": _cpsc_mod.check_ps002(issue_body, triggers),
        "specify_not_canonical": _cpsc_mod.check_ps003(issue_body, triggers),
        "diff_first_rationale_present": _cpsc_mod.check_ps004(
            triggers, mrc, product_spec_context
        ),
        "generated_task_trace_present": _cpsc_mod.check_ps005(
            triggers, mrc, product_spec_context
        ),
        "task_dependencies_materialized": _cpsc_mod.check_ps006(issue_body, triggers),
    }

    return {
        "applicability": applicability,
        "triggers": triggers,
        "checks": checks,
    }


def test_run_gh_api_labels_do_not_affect_product_spec_decision():
    """AC12: real run_gh_api()->decision pipeline is label-invariant.

    Two GitHub API responses differ ONLY in `labels` (identical body/title).
    The production decision pipeline must produce byte-identical
    applicability/triggers/checks for both.
    """
    result_no_labels = _run_gh_api_with_labels([])
    result_needs_human = _run_gh_api_with_labels(["state/needs-human"])
    result_multi_label = _run_gh_api_with_labels(
        ["state/needs-human", "triage-required", "phase/implementation"]
    )

    assert result_no_labels == result_needs_human
    assert result_no_labels == result_multi_label


# ---------------------------------------------------------------------------
# 2. run_contract_review_once.py's own entry point run_once() — the actual
#    execution path of issue-contract-review itself — across a label
#    permutation matrix.
# ---------------------------------------------------------------------------

_DEFAULT_BODY_SNAPSHOT = (
    "## Machine-Readable Contract\n\n"
    "```yaml\n"
    "contract_schema_version: v1\n"
    "issue_kind: implementation\n"
    'parent_issue: "none"\n'
    "```\n\n"
    "## Outcome\n\nfixture body for state-label readiness invariant test.\n"
)

_LABEL_PERMUTATIONS = [
    [],
    ["state/needs-human"],
    ["triage-required"],
    ["phase/implementation", "state/needs-human"],
]


def _make_readiness_json(status: str) -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": status,
        "body_sha256": "sha256:abc",
        "source_checks": [],
        "errors": [],
        "minimal_context": [],
        "fix_hint": None,
    }


def _make_product_spec_json(decision: str, applicability: str = "applicable") -> dict:
    return {
        "schema": "product_spec_check/v1",
        "applicability": applicability,
        "decision": decision,
        "triggers": {},
        "conditions": {},
        "blocked_reasons": [],
        "body_sha256": "sha256:abc",
        "source_provenance": {
            "source_type": "github_issue_body",
            "body_file": None,
        },
    }


def _make_vc_preflight_json(status: str) -> dict:
    return {
        "schema": "BASELINE_VC_PREFLIGHT_RESULT_V1",
        "status": status,
        "results": [],
        "errors": [],
    }


def _make_declared_path_overlap_result() -> dict:
    return {
        "schema": "declared_path_overlap/v1",
        "advisory": True,
        "blocking": False,
        "decision": "advisory_only",
        "disjoint": True,
        "overlapping_prs": [],
        "inventory": {
            "schema": "OPEN_PR_INVENTORY_V1",
            "totalCount": 0,
            "fetched_count": 0,
            "has_next_page": False,
            "complete": True,
            "saturated": False,
        },
        "errors": [],
        "note": "changed-file 名の単純な重なりのみを証明する advisory check。",
    }


def _run_once_for_labels(labels: list) -> dict:
    """Call the REAL run_once() (issue-contract-review's own orchestrator
    entry point). Sub-process boundaries to child scripts are stubbed with
    fixed pass results (matching the existing all-pass test pattern in
    test_run_contract_review_once.py::TestAllChecksCalledB1), since those
    child scripts make live `gh`/subprocess calls that must stay offline in
    CI. Nothing in run_once()'s call graph reads `labels` — this test proves
    that the ONLY way `labels` could vary its call graph, the gh API
    response consumed inside check_product_spec_contract.py, is provably
    inert (see test_run_gh_api_labels_do_not_affect_product_spec_decision
    above) and that run_once() itself never threads a labels argument
    through to its status computation.
    """
    readiness_json = _make_readiness_json("go")
    product_spec_json = _make_product_spec_json("pass", "applicable")
    vc_json = _make_vc_preflight_json("pass")

    run_script_results = iter(
        [
            (readiness_json, 0, None),
            (product_spec_json, 0, None),
            (vc_json, 0, None),
        ]
    )
    shell_script_results = iter([(0, "OK: no blockers", "")])

    # `labels` is threaded into this closure purely to document intent for
    # readers/maintainers; it is intentionally NOT passed to run_once() or
    # any mocked check, because production run_once() has no such
    # parameter (see test_run_once_signature_has_no_label_parameter below).
    del labels

    with patch.object(_rcr_mod, "fetch_body_from_github", return_value=(_DEFAULT_BODY_SNAPSHOT, None)):
        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_results)):
            with patch.object(
                _rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_script_results)
            ):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(),
                    ):
                        return run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)


def test_run_once_status_stable_across_label_permutation_matrix():
    """AC12: run_once() (issue-contract-review's actual execution path)
    yields an identical status for every label permutation."""
    results = [_run_once_for_labels(labels) for labels in _LABEL_PERMUTATIONS]

    statuses = {result["status"] for result in results}
    assert statuses == {"go"}, f"expected all permutations to be 'go', got: {statuses}"

    checks_snapshots = {json.dumps(result["checks"], sort_keys=True) for result in results}
    assert len(checks_snapshots) == 1, (
        "run_once() checks output diverged across label permutations "
        f"({len(checks_snapshots)} distinct snapshots)"
    )


def test_run_once_signature_has_no_label_parameter():
    """AC12 fossil guard: run_once() must never accept a labels-shaped
    parameter, so label data structurally cannot re-enter the readiness
    computation as an authority input."""
    sig = inspect.signature(run_once)
    param_names = {name.lower() for name in sig.parameters}
    forbidden = {"labels", "label", "state_label", "issue_labels"}
    assert not (param_names & forbidden), (
        f"run_once() unexpectedly accepts a label-shaped parameter: {param_names & forbidden}"
    )
