"""
tests/test_run_contract_review_once.py

Unit tests for run_contract_review_once.py

AC6: run_contract_review_once.py の unit test が PASS する

B1: run_once() から check_blockers.sh / check_product_spec_contract.py /
    baseline_vc_preflight.py が全て呼ばれる（不正時は blocked/human_judgment を返す）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_RCR_PATH = _SCRIPTS_DIR / "run_contract_review_once.py"

spec = importlib.util.spec_from_file_location("run_contract_review_once", _RCR_PATH)
assert spec is not None and spec.loader is not None
_rcr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_rcr_mod)  # type: ignore[union-attr]

run_once = _rcr_mod.run_once
classify_http_error = _rcr_mod.classify_http_error
HTTP_ERROR_CLASSIFICATIONS = _rcr_mod.HTTP_ERROR_CLASSIFICATIONS

_ISSUE_NUMBER = 817
_REPO = "squne121/loop-protocol"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"

# Issue #1914 P0-3: run_once() now fetches the Issue body exactly once, at
# the very start of every invocation, before Step 1's idempotency check even
# runs (see run_contract_review_once.py module docstring). None of the
# existing tests in this file exercise body content directly (they intercept
# _run_script's parsed JSON output), so a single generic default body is
# supplied here for every test in this file. Tests that need to control the
# fetch (e.g. a fetch failure) patch fetch_body_from_github themselves
# inside their own `with` block, which takes precedence over this default
# fixture for the duration of that block.
_DEFAULT_BODY_SNAPSHOT = (
    "## Machine-Readable Contract\n\n"
    "```yaml\n"
    "contract_schema_version: v1\n"
    "issue_kind: implementation\n"
    'parent_issue: "none"\n'
    "```\n\n"
    "## Outcome\n\nfixture body for run_contract_review_once unit tests.\n"
)


@pytest.fixture(autouse=True)
def _default_body_snapshot_fetch():
    with patch.object(
        _rcr_mod, "fetch_body_from_github", return_value=(_DEFAULT_BODY_SNAPSHOT, None)
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_current_head_vc_preflight_json(status: str = "pass") -> dict:
    """Return a producer-certified current-head envelope for caller tests."""
    head = "a" * 40
    return {
        "schema": "baseline_vc_preflight/v1",
        "generated_at": "2026-07-12T00:00:00Z",
        "status": status,
        "errors": [],
        "source": {"kind": "body_file", "body_sha256": "sha256:" + "b" * 64},
        "results": [],
        "evidence_mode": "current-head",
        "head_sha": head,
        "reviewed_head_sha": head,
        "head_after_sha": head,
        "clean_before": True,
        "clean_after": True,
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
    }


def _make_declared_path_overlap_result(disjoint: bool = True) -> dict:
    """declared_path_overlap（advisory のみ、Issue #1680）の固定 stub 結果。"""
    overlapping_prs = [] if disjoint else [
        {
            "pr_number": 9999,
            "url": "https://github.com/squne121/loop-protocol/pull/9999",
            "head_ref_oid": "c" * 40,
            "is_draft": False,
            "is_cross_repository": False,
            "matched_files": [".claude/skills/issue-contract-review/SKILL.md"],
        }
    ]
    return {
        "schema": "declared_path_overlap/v1",
        "advisory": True,
        "blocking": False,
        "decision": "advisory_only",
        "disjoint": disjoint,
        "overlapping_prs": overlapping_prs,
        "inventory": {
            "schema": "OPEN_PR_INVENTORY_V1",
            "totalCount": len(overlapping_prs),
            "fetched_count": len(overlapping_prs),
            "has_next_page": False,
            "complete": True,
            "saturated": False,
        },
        "errors": [],
        "note": "changed-file 名の単純な重なりのみを証明する advisory check。",
    }


def _make_subprocess_result(stdout: str, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""
    result.returncode = returncode
    return result


def _make_all_pass_side_effects():
    """
    Return side_effect iterables for _run_script and _run_shell_script
    that simulate all checks passing.

    _run_script call order:
      1. contract_readiness_check.py → go
      2. check_product_spec_contract.py → pass
      3. baseline_vc_preflight.py → pass

    _run_shell_script call order:
      1. check_blockers.sh → exit 0
    """
    readiness_json = _make_readiness_json("go")
    product_spec_json = _make_product_spec_json("pass", "applicable")
    vc_json = _make_vc_preflight_json("pass")

    run_script_results = [
        (readiness_json, 0, None),   # readiness
        (product_spec_json, 0, None),  # product_spec
        (vc_json, 0, None),           # vc_preflight
    ]
    shell_script_results = [
        (0, "OK: no blockers", ""),  # check_blockers.sh
    ]
    return run_script_results, shell_script_results


# ---------------------------------------------------------------------------
# B1: all four checks are called
# ---------------------------------------------------------------------------


class TestAllChecksCalledB1:
    """B1: run_once calls readiness, blockers, product_spec, and vc_preflight."""

    def test_all_four_checks_called_on_go(self, monkeypatch):
        """When all checks pass, all four are invoked and status is go."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["checks"]["readiness"] == "go"
        assert result["checks"]["blockers"] == "pass"
        assert result["checks"]["product_spec"] == "pass"
        assert result["checks"]["product_spec_check"] == _make_product_spec_json(
            "pass", "applicable"
        )
        assert result["checks"]["vc_preflight"] == "pass"
        assert result["checks"]["declared_path_overlap"]["disjoint"] is True
        assert result["checks"]["declared_path_overlap"]["advisory"] is True
        assert result["checks"]["declared_path_overlap"]["blocking"] is False


    def test_current_head_arguments_are_forwarded_to_producer(self):
        """GIVEN certified current-head input WHEN review runs THEN it preserves the full envelope."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_script_results[-1] = (_make_current_head_vc_preflight_json(), 0, None)
        captured = []

        def run_script(*args, **kwargs):
            captured.append(args[0])
            return run_script_results.pop(0)

        with patch.object(_rcr_mod, "_run_script", side_effect=run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=shell_results[0]):
                with patch.object(
                    _rcr_mod,
                    "_run_declared_path_overlap_check",
                    return_value=_make_declared_path_overlap_result(disjoint=True),
                ):
                    result = run_once(
                        _ISSUE_NUMBER, _REPO, skip_idempotency_check=True,
                        evidence_mode="current-head", cwd="/tmp/pr-worktree", reviewed_head_sha="a" * 40,
                    )

        producer_command = captured[-1]
        assert result["status"] == "go"
        assert producer_command[-8:] == [
            "--cwd", "/tmp/pr-worktree", "--evidence-mode", "current-head",
            "--reviewed-head-sha", "a" * 40, "--format", "json",
        ]
        assert result["current_vc_result"] == _make_current_head_vc_preflight_json()
        assert result["vc_evidence"]["schema"] == "baseline_vc_preflight/v1"
        assert result["vc_evidence"]["source"]["body_sha256"].startswith("sha256:")

    def test_current_head_rejects_malformed_pass_envelope(self):
        """GIVEN malformed current-head PASS WHEN review runs THEN caller blocks it."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        malformed = _make_current_head_vc_preflight_json()
        malformed["schema"] = "wrong/v1"
        malformed.pop("results")
        run_script_results[-1] = (malformed, 0, None)

        with patch.object(_rcr_mod, "_run_script", side_effect=run_script_results):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=shell_results[0]):
                result = run_once(
                    _ISSUE_NUMBER,
                    _REPO,
                    skip_idempotency_check=True,
                    evidence_mode="current-head",
                    cwd="/tmp/pr-worktree",
                    reviewed_head_sha="a" * 40,
                )

        assert result["status"] == "blocked"
        assert result["checks"]["vc_preflight"] == "blocked"
        assert result["current_vc_result"] == malformed
        assert any(error.startswith("uncertified_current_head_vc_evidence:") for error in result["errors"])

    def test_blockers_blocked_stops_pipeline(self, monkeypatch):
        """If check_blockers.sh returns exit 1 (open blockers), status: blocked."""
        readiness_json = _make_readiness_json("go")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 0, None)):
            with patch.object(
                _rcr_mod, "_run_shell_script",
                return_value=(1, "", "human_escalation: blocker open"),
            ):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "check_blockers"
        assert result["checks"]["blockers"] == "blocked"

    def test_blockers_human_judgment(self, monkeypatch):
        """check_blockers.sh returns 'human_escalation: native API unavailable' → human_judgment."""
        readiness_json = _make_readiness_json("go")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 0, None)):
            with patch.object(
                _rcr_mod, "_run_shell_script",
                return_value=(1, "", "human_escalation: native dependency API unavailable"),
            ):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "check_blockers"
        assert result["checks"]["blockers"] == "human_judgment"

    def test_product_spec_fail_blocked(self, monkeypatch):
        """check_product_spec_contract.py applicable+fail → blocked."""
        readiness_json = _make_readiness_json("go")
        product_spec_fail = _make_product_spec_json("fail", "applicable")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_fail, 1, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "product_spec_check"
        assert result["checks"]["product_spec"] == "fail"

    def test_product_spec_human_judgment(self, monkeypatch):
        """check_product_spec_contract.py applicable+human_judgment → human_judgment."""
        readiness_json = _make_readiness_json("go")
        product_spec_hj = _make_product_spec_json("human_judgment", "applicable")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_hj, 1, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "product_spec_check"
        assert result["checks"]["product_spec"] == "human_judgment"

    def test_product_spec_not_applicable_treated_as_pass(self, monkeypatch):
        """check_product_spec_contract.py not_applicable → treated as pass, pipeline continues."""
        readiness_json = _make_readiness_json("go")
        product_spec_na = _make_product_spec_json("pass", "not_applicable")
        vc_json = _make_vc_preflight_json("pass")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_na, 0, None),
            (vc_json, 0, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["checks"]["product_spec"] == "pass"

    def test_vc_preflight_blocked_stops(self, monkeypatch):
        """baseline_vc_preflight blocked → status: blocked."""
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_blocked = _make_vc_preflight_json("blocked")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_json, 0, None),
            (vc_blocked, 1, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "vc_preflight"
        assert result["checks"]["vc_preflight"] == "blocked"

    def test_vc_preflight_human_judgment(self, monkeypatch):
        """baseline_vc_preflight human_judgment → status: human_judgment."""
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_hj = _make_vc_preflight_json("human_judgment")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_json, 0, None),
            (vc_hj, 2, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "vc_preflight"


# ---------------------------------------------------------------------------
# Issue #1631: readiness timeout is independently configurable
# ---------------------------------------------------------------------------


class TestReadinessTimeout:
    """GIVEN readiness execution WHEN timeout is configured THEN only Step 2 uses it."""

    def test_default_exceeds_generic_timeout_and_applies_only_to_readiness(self):
        run_script_results, shell_results = _make_all_pass_side_effects()
        calls = []

        def fake_run_script(cmd, timeout=_rcr_mod._DEFAULT_TIMEOUT):
            calls.append((cmd, timeout))
            return run_script_results.pop(0)

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=shell_results[0]):
                with patch.object(
                    _rcr_mod,
                    "_run_declared_path_overlap_check",
                    return_value=_make_declared_path_overlap_result(disjoint=True),
                ):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert _rcr_mod._DEFAULT_READINESS_TIMEOUT_SECONDS > _rcr_mod._DEFAULT_TIMEOUT
        assert calls[0][1] == _rcr_mod._DEFAULT_READINESS_TIMEOUT_SECONDS
        assert calls[1][1] == _rcr_mod._DEFAULT_TIMEOUT
        assert calls[2][1] == _rcr_mod._VC_PREFLIGHT_TIMEOUT

    def test_override_is_forwarded_to_readiness_and_reported_on_timeout(self):
        applied_timeout_seconds = 47
        captured = []

        def timeout_readiness(cmd, timeout=_rcr_mod._DEFAULT_TIMEOUT):
            captured.append((cmd, timeout))
            return None, -1, "timeout"

        with patch.object(_rcr_mod, "_run_script", side_effect=timeout_readiness):
            result = run_once(
                _ISSUE_NUMBER,
                _REPO,
                skip_idempotency_check=True,
                readiness_timeout_seconds=applied_timeout_seconds,
            )

        assert result["status"] == "runtime_error"
        assert captured[0][1] == applied_timeout_seconds
        assert result["errors"] == [
            "readiness_check_error: timeout (readiness_timeout_seconds=47)"
        ]

    def test_cli_wires_readiness_timeout_override(self):
        with patch.object(_rcr_mod, "run_once", return_value={"status": "go"}) as mocked:
            with patch.object(
                sys,
                "argv",
                [
                    "run_contract_review_once.py",
                    "--issue-number",
                    str(_ISSUE_NUMBER),
                    "--readiness-timeout-seconds",
                    "47",
                ],
            ):
                assert _rcr_mod.main() == 0

        assert mocked.call_args.kwargs["readiness_timeout_seconds"] == 47

    @pytest.mark.parametrize("invalid_value", ["0", "-1"])
    def test_cli_rejects_nonpositive_readiness_timeout(self, invalid_value):
        with patch.object(
            sys,
            "argv",
            [
                "run_contract_review_once.py",
                "--issue-number",
                str(_ISSUE_NUMBER),
                "--readiness-timeout-seconds",
                invalid_value,
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                _rcr_mod.main()

        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Status routing tests
# ---------------------------------------------------------------------------


class TestDeclaredPathOverlapAdvisoryOnly:
    """Issue #1680: declared_path_overlap is advisory only and never blocks."""

    def test_disjoint_open_pr_continues_go(self, monkeypatch):
        """AC2: OPEN PR が存在しても changed files が Allowed Paths と disjoint なら go を継続する。"""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["checks"]["declared_path_overlap"]["disjoint"] is True

    def test_overlapping_open_pr_does_not_block_go(self, monkeypatch):
        """AC1/AC3: OPEN PR の changed-file 名重複（非 disjoint）だけでは blocked にならない。"""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=False),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        # declared_path_overlap は advisory のみ: disjoint=False (overlap あり)
        # でも status は go のまま — 単独では blocking にしない。
        assert result["status"] == "go"
        assert result["checks"]["declared_path_overlap"]["disjoint"] is False
        assert result["checks"]["declared_path_overlap"]["advisory"] is True
        assert result["checks"]["declared_path_overlap"]["blocking"] is False
        assert len(result["checks"]["declared_path_overlap"]["overlapping_prs"]) == 1

    def test_declared_path_overlap_contract_violation_forced_advisory(self, monkeypatch):
        """AC3: check の advisory/blocking フラグが崩れていても呼び出し側が安全側に強制する。"""
        broken_result = _make_declared_path_overlap_result(disjoint=False)
        broken_result["advisory"] = False
        broken_result["blocking"] = True

        with patch(
            "declared_path_overlap.compute_declared_path_overlap_for_issue",
            create=True,
            return_value=broken_result,
        ):
            checked = _rcr_mod._run_declared_path_overlap_check(_ISSUE_NUMBER, _REPO)

        assert checked["advisory"] is True
        assert checked["blocking"] is False
        assert any(
            "declared_path_overlap_contract_violation_forced_advisory" in e
            for e in checked["errors"]
        )

    def test_producer_exception_degrades_to_unavailable_advisory(self, monkeypatch):
        """P0-3: an uncaught exception in the producer must not propagate out of
        _run_declared_path_overlap_check (and therefore not out of run_once(),
        which calls this before result["status"] is set to "go")."""

        def raise_boom(*args, **kwargs):
            raise RuntimeError("boom: transient gh failure")

        with patch(
            "declared_path_overlap.compute_declared_path_overlap_for_issue",
            create=True,
            side_effect=raise_boom,
        ):
            checked = _rcr_mod._run_declared_path_overlap_check(_ISSUE_NUMBER, _REPO)

        assert checked["advisory"] is True
        assert checked["blocking"] is False
        assert checked["decision"] == "unavailable"
        assert checked["disjoint"] is None
        assert any(
            "declared_path_overlap_internal_exception" in e for e in checked["errors"]
        )

    def test_producer_exception_does_not_abort_run_once(self, monkeypatch):
        """P0-3: run_once() as a whole must still reach status: go and emit its
        JSON contract even when the declared_path_overlap producer explodes."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        def raise_boom(*args, **kwargs):
            raise RuntimeError("boom")

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch(
                        "declared_path_overlap.compute_declared_path_overlap_for_issue",
                        create=True,
                        side_effect=raise_boom,
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["checks"]["declared_path_overlap"]["decision"] == "unavailable"
        assert result["checks"]["declared_path_overlap"]["disjoint"] is None

    def test_base_ref_forwarded_to_producer(self, monkeypatch):
        """P1-3: the wrapper must scope the OPEN PR inventory to the same base
        branch ("main") as the Allowed Paths review, not leave it unset."""
        captured = {}

        def fake_compute(issue_number, repo, base_ref=None, **kwargs):
            captured["issue_number"] = issue_number
            captured["repo"] = repo
            captured["base_ref"] = base_ref
            return _make_declared_path_overlap_result(disjoint=True)

        with patch(
            "declared_path_overlap.compute_declared_path_overlap_for_issue",
            create=True,
            side_effect=fake_compute,
        ):
            _rcr_mod._run_declared_path_overlap_check(_ISSUE_NUMBER, _REPO)

        assert captured["base_ref"] == "main"
        assert captured["issue_number"] == _ISSUE_NUMBER
        assert captured["repo"] == _REPO


class TestStatusRouting:
    """Test that run_once correctly routes based on readiness status."""

    def test_readiness_go_returns_go(self, monkeypatch):
        """Readiness check returns go (all others also pass) → status: go."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["source"] == "all_checks_pass"

    def test_readiness_needs_fix_returns_blocked(self, monkeypatch):
        """Readiness check returns needs_fix → status: blocked (pipeline stops)."""
        readiness_json = _make_readiness_json("needs_fix")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 1, None)):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "readiness_check"

    def test_readiness_human_judgment_returns_human_judgment(self, monkeypatch):
        """Readiness check returns human_judgment → status: human_judgment."""
        readiness_json = _make_readiness_json("human_judgment")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 2, None)):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_check"

    def test_readiness_unknown_status_returns_runtime_error(self, monkeypatch):
        """Unknown readiness status → runtime_error (not human_judgment)."""
        readiness_json = _make_readiness_json("totally_unknown_status")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 5, None)):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert any("unknown_readiness_status" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# JSON parse failure → runtime_error (not human_judgment)
# ---------------------------------------------------------------------------


class TestJsonParseFailure:
    """AC design: subprocess JSON parse failure → runtime_error, NOT human_judgment."""

    def test_json_parse_failure_is_runtime_error(self, monkeypatch):
        """Corrupt JSON from readiness check → runtime_error."""

        def fake_run_script(cmd, timeout=30):
            return (None, 0, "json_parse_error: Expecting value: line 1 column 1")

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error", (
            "JSON parse failure must be runtime_error, not human_judgment"
        )
        assert any("readiness_check_error" in e for e in result["errors"])

    def test_json_parse_failure_not_human_judgment(self, monkeypatch):
        """JSON parse failure must NOT produce human_judgment status."""

        def fake_run_script(cmd, timeout=30):
            return (None, 1, "json_parse_error: unexpected end")

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] != "human_judgment", (
            "JSON parse failure must never produce human_judgment"
        )


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------


class TestIdempotencyCheck:
    """Test that existing go comment is returned without running review."""

    def test_existing_go_deduped(self, monkeypatch):
        """If existing go comment found → return early with deduped."""
        existing_url = f"{_ISSUE_URL}#issuecomment-1001"
        existing_go = {
            "html_url": existing_url,
            "inner": {"checks": {"product_spec_check": _make_product_spec_json("pass")}},
        }

        with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(existing_go, None)):
            with patch.object(
                _rcr_mod,
                "_run_declared_path_overlap_check",
                return_value=_make_declared_path_overlap_result(disjoint=True),
            ) as overlap_check:
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=False)

        assert result["status"] == "go"
        assert result["source"] == "existing_go_comment"
        assert result["checks"]["product_spec_check"]["schema"] == "product_spec_check/v1"
        assert result["go_comment_url"] == existing_url
        assert result["idempotency_check"]["deduped"] is True
        # P0-2 (#1794 PR review): declared_path_overlap is a volatile,
        # OPEN-PR-live observation and must be recomputed fresh even on the
        # existing-go reuse path, never replayed from the saved comment.
        overlap_check.assert_called_once_with(_ISSUE_NUMBER, _REPO)
        assert result["checks"]["declared_path_overlap"]["disjoint"] is True

    def test_existing_go_deduped_recomputes_declared_path_overlap_fresh(self, monkeypatch):
        """AC (P0-2): reuse path must recompute declared_path_overlap, not replay a
        stale saved value even when the saved comment carried a different result."""
        existing_url = f"{_ISSUE_URL}#issuecomment-1002"
        stale_overlap = _make_declared_path_overlap_result(disjoint=True)
        existing_go = {
            "html_url": existing_url,
            "inner": {
                "checks": {
                    "product_spec_check": _make_product_spec_json("pass"),
                    "declared_path_overlap": stale_overlap,
                }
            },
        }
        fresh_overlap = _make_declared_path_overlap_result(disjoint=False)

        with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(existing_go, None)):
            with patch.object(
                _rcr_mod, "_run_declared_path_overlap_check", return_value=fresh_overlap
            ) as overlap_check:
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=False)

        overlap_check.assert_called_once_with(_ISSUE_NUMBER, _REPO)
        # status stays go: declared_path_overlap is advisory only and never
        # blocks, even when the freshly recomputed value shows an overlap.
        assert result["status"] == "go"
        assert result["checks"]["declared_path_overlap"] == fresh_overlap
        assert result["checks"]["declared_path_overlap"]["disjoint"] is False

    def test_idempotency_check_error_non_fatal(self, monkeypatch):
        """Idempotency check error → non-fatal, continue with review."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, "gh_timeout")):
            with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
                with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=False)

        # Error recorded but not fatal
        assert any("idempotency_check_error" in e for e in result["errors"])
        assert result["status"] == "go"  # Review still ran


# ---------------------------------------------------------------------------
# HTTP error classification
# ---------------------------------------------------------------------------


class TestHttpErrorClassification:
    """403/429/422 classification for contract review API calls."""

    def test_403_permission_denied(self):
        assert classify_http_error(403) == "permission_denied"

    def test_429_rate_limited(self):
        assert classify_http_error(429) == "rate_limited"

    def test_422_validation_failed(self):
        assert classify_http_error(422) == "validation_failed_or_spam"

    def test_unknown_ambiguous(self):
        assert classify_http_error(500) == "ambiguous_no_retry"
        assert classify_http_error(503) == "ambiguous_no_retry"

    def test_classification_table_complete(self):
        """Ensure all critical error codes are mapped."""
        assert 403 in HTTP_ERROR_CLASSIFICATIONS
        assert 429 in HTTP_ERROR_CLASSIFICATIONS
        assert 422 in HTTP_ERROR_CLASSIFICATIONS


# ---------------------------------------------------------------------------
# run_script helper
# ---------------------------------------------------------------------------


class TestRunScriptHelper:
    """Tests for _run_script error handling."""

    def test_timeout_returns_error(self, monkeypatch):
        """Timeout → error code, not human_judgment."""

        def fake_run_script(cmd, timeout=30):
            return (None, -1, "timeout")

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert any("timeout" in e for e in result["errors"])

    def test_no_output_returns_runtime_error(self, monkeypatch):
        """No output from readiness check → runtime_error."""

        def fake_run_script(cmd, timeout=30):
            return (None, 0, None)

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"


# ---------------------------------------------------------------------------
# Schema output validation
# ---------------------------------------------------------------------------


class TestSchemaOutput:
    """Ensure CONTRACT_REVIEW_ONCE_RESULT_V1 schema fields are present."""

    def test_schema_fields_present(self, monkeypatch):
        """All required fields present in output including checks (B1)."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    with patch.object(
                        _rcr_mod,
                        "_run_declared_path_overlap_check",
                        return_value=_make_declared_path_overlap_result(disjoint=True),
                    ):
                        result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        required_fields = [
            "schema",
            "issue_number",
            "repo",
            "mode",
            "status",
            "source",
            "go_comment_url",
            "readiness_status",
            "readiness_errors",
            "checks",
            "idempotency_check",
            "errors",
        ]
        for field in required_fields:
            assert field in result, f"Missing required field: {field}"

        assert result["schema"] == "CONTRACT_REVIEW_ONCE_RESULT_V1"

        # B1: checks sub-fields
        assert "readiness" in result["checks"]
        assert "blockers" in result["checks"]
        assert "product_spec" in result["checks"]
        assert "vc_preflight" in result["checks"]
