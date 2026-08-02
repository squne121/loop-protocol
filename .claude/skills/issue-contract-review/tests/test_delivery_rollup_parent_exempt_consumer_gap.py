"""
tests/test_delivery_rollup_parent_exempt_consumer_gap.py

Issue #1914 P0-1 (#1940 adversarial review, fix_delta iteration 4):

A read-only regression test proving (or disproving) that a real downstream
consumer of ``CONTRACT_REVIEW_ONCE_RESULT_V1`` distinguishes the
delivery-rollup-parent-exempt shape (``status: go``, ``applicability:
not_applicable``) from a normal VC-preflight-satisfied ``go``.

This test imports and exercises the ACTUAL consumer function
``ensure_contract_snapshot()`` in
``.claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py`` — it
does not reimplement or stub out the consumer's decision logic. Only the
network-touching collaborators (``fetch_issue_snapshot``,
``_import_parser_module``, ``run_contract_review_once``) are mocked, exactly
as the existing test suite in
``.claude/skills/impl-review-loop/tests/test_ensure_contract_snapshot.py``
already does for other scenarios (see ``test_stale_go_auto_without_post_
materializes_dry_run`` for the closest precedent this test's mocking
follows).

This file lives in ``.claude/skills/issue-contract-review/tests/`` (Issue
#1914's Allowed Paths) and is READ-ONLY with respect to
``ensure_contract_snapshot.py`` — it imports that module but does not edit
it. ``ensure_contract_snapshot.py`` itself is outside this Issue's Allowed
Paths and is intentionally NOT modified here.

KNOWN_GAP finding (honest, not papered over): as of this writing,
``ensure_contract_snapshot()`` reads only ``review_result.get("status")``
from the ``CONTRACT_REVIEW_ONCE_RESULT_V1`` produced by
``run_contract_review_once()`` when deciding whether to proceed past the
"review_status == go" branch (see ``ensure_contract_snapshot.py`` around the
``review_status != "go"`` check). It does NOT inspect the ``applicability``
field at all. Consequently, a delivery-rollup-parent-exempt result
(``status: go``, ``applicability: not_applicable``) is currently treated
IDENTICALLY to a normal VC-preflight-satisfied ``go`` by this consumer: it
proceeds to ``dry_run_would_post`` / posting a snapshot exactly as if VC
preflight had actually run and passed.

This test asserts the CURRENT (unsafe) behavior explicitly, rather than
silently passing as if the gap were closed. Tracked in follow-up #1944.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Import ensure_contract_snapshot.py directly from its actual location
# (impl-review-loop/scripts/), matching the importlib-based loading pattern
# already used by test_ensure_contract_snapshot.py /
# test_contract_snapshot_author_binding.py. This is READ-ONLY: no file under
# impl-review-loop/ is written by this test.
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_IMPL_REVIEW_LOOP_SCRIPTS_DIR = (
    _HERE.parent.parent / "impl-review-loop" / "scripts"
)
_ECS_PATH = _IMPL_REVIEW_LOOP_SCRIPTS_DIR / "ensure_contract_snapshot.py"

assert _ECS_PATH.exists(), f"consumer script not found: {_ECS_PATH}"

_spec = importlib.util.spec_from_file_location(
    "ensure_contract_snapshot_delivery_rollup_gap_check", _ECS_PATH
)
assert _spec is not None and _spec.loader is not None
_ecs_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ecs_mod)  # type: ignore[union-attr]

ensure_contract_snapshot = _ecs_mod.ensure_contract_snapshot
sha256_of = _ecs_mod.sha256_of

_ISSUE_NUMBER = 1914
_REPO = "squne121/loop-protocol"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"

_SAMPLE_BODY = (
    "## Machine-Readable Contract\n\n"
    "```yaml\n"
    "contract_schema_version: v1\n"
    "issue_kind: parent\n"
    "parent_mode: delivery-rollup\n"
    "closure_mode: child-complete\n"
    "```\n"
)
_SAMPLE_BODY_SHA256 = sha256_of(_SAMPLE_BODY)
_SAMPLE_UPDATED_AT = "2026-06-13T08:00:00Z"


def _delivery_rollup_exempt_review_result() -> dict:
    """A CONTRACT_REVIEW_ONCE_RESULT_V1-shaped result for the
    delivery-rollup-parent-exempt case (Issue #1914 P0-1): ``status: go``
    with ``applicability: not_applicable`` because the Final Gate's VC
    preflight requirement does not apply, NOT because it was satisfied.
    """
    return {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "status": "go",
        "applicability": "not_applicable",
        "readiness_status": "go",
        "checks": {
            "readiness": "go",
            "blockers": "pass",
            "product_spec": "pass",
            "product_spec_check": None,
            "vc_preflight": "not_applicable",
        },
        "vc_preflight_status": "not_applicable",
        "vc_preflight_classifications": [],
        "errors": [],
    }


def _mock_parser_mod_no_go() -> MagicMock:
    """A parser module double reporting no existing go/blocked comment,
    forcing ensure_contract_snapshot() to call the (mocked)
    run_contract_review_once() producer.
    """
    mod = MagicMock()
    mod.fetch_issue_comments.return_value = ([], None)
    mod.parse_contract_review_results.return_value = []
    mod.find_latest_result.return_value = None
    mod.find_latest_go.return_value = None
    mod.find_latest_authoritative_go.return_value = None
    return mod


def test_delivery_rollup_exempt_result_is_not_distinguished_by_ensure_contract_snapshot_consumer():
    """
    KNOWN_GAP (tracked in follow-up #1944): ensure_contract_snapshot() does
    not inspect ``applicability`` and therefore currently treats a
    delivery-rollup-parent-exempt ``status: go`` identically to a normal
    VC-preflight-satisfied ``go``. This test pins down and documents that
    CURRENT behavior (it does not assert the gap is fixed).

    If a future change teaches ensure_contract_snapshot() (or a successor
    consumer) to read ``applicability`` and refuse to treat
    ``not_applicable`` as implementation-ready by itself, this test's
    assertion below will start failing and must be updated together with
    closing follow-up #1944 -- do not silently loosen this assertion without
    also updating the KNOWN_GAP narrative above.
    """
    parser_mod = _mock_parser_mod_no_go()

    with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
        with patch.object(
            _ecs_mod,
            "fetch_issue_snapshot",
            return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
        ):
            with patch.object(
                _ecs_mod,
                "run_contract_review_once",
                return_value=(_delivery_rollup_exempt_review_result(), None),
            ) as review:
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="auto",
                    do_post=False,
                )

    review.assert_called_once()

    # KNOWN_GAP: the consumer proceeds to "dry_run_would_post" (its
    # implementation-ready / would-dispatch signal for do_post=False) exactly
    # as it would for an ordinary satisfied-VC go, because it never reads
    # the applicability field on review_result. This is the unresolved gap
    # the human reviewer flagged for Issue #1914 P0-1 and that follow-up
    # #1944 must address (e.g. by teaching ensure_contract_snapshot(), or
    # whichever consumer is authoritative, to require
    # applicability == "applicable" before treating "go" as VC-satisfied).
    assert result["status"] == "dry_run_would_post", (
        "KNOWN_GAP regression sentinel: ensure_contract_snapshot() no longer "
        "proceeds to dry_run_would_post for a not_applicable-applicability "
        "go result. If this now fails because the gap was closed, update "
        "this test's assertions and docstring to reflect the fixed "
        "behavior and close follow-up #1944 accordingly, instead of just "
        "deleting or loosening this assertion."
    )
    assert result["source"] == "materialized_go"
