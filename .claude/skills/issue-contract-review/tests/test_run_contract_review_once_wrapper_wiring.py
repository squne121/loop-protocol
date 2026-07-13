"""
tests/test_run_contract_review_once_wrapper_wiring.py

Issue #1333 AC2 / AC3 / AC4:

AC2: run_contract_review_once.py の _VC_PREFLIGHT_TIMEOUT が、per-command
     timeout の named constant を参照した関係式(per-command timeout ×
     想定コマンド数 + overhead の named constant)として定義され、単純な
     独立引き上げ値になっていないことを固定する。

AC3: run_contract_review_once.py が baseline_vc_preflight.py を subprocess
     起動する際、--timeout-seconds を明示的な argv として渡すことを固定する。

Runtime Verification Applicability: not_applicable
named constant の値・関係・argv 構築のみを検証する軽量テストであり、
実際に長時間 VC を実行する timing test は含まない(baseline_vc_preflight.py
の subprocess 呼び出し自体は patch.object でモックする)。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Import module under test (既存 test_run_contract_review_once.py と同じ
# importlib ベースのロード方式に合わせる)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_RCR_PATH = _SCRIPTS_DIR / "run_contract_review_once.py"

spec = importlib.util.spec_from_file_location("run_contract_review_once", _RCR_PATH)
assert spec is not None and spec.loader is not None
_rcr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_rcr_mod)  # type: ignore[union-attr]

run_once = _rcr_mod.run_once

_ISSUE_NUMBER = 1333
_REPO = "squne121/loop-protocol"


# ---------------------------------------------------------------------------
# Helpers (既存 test_run_contract_review_once.py の fixture 生成パターンを踏襲)
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


# ---------------------------------------------------------------------------
# AC2: _VC_PREFLIGHT_TIMEOUT is a derived relationship, not an independent literal
# ---------------------------------------------------------------------------


class TestVcPreflightTimeoutRelationshipAC2:
    """AC2: _VC_PREFLIGHT_TIMEOUT は per-command timeout との関係式で定義される。"""

    def test_per_command_timeout_matches_baseline_vc_preflight_constant(self):
        """drift 防止: _VC_PREFLIGHT_PER_COMMAND_TIMEOUT は
        baseline_vc_preflight.DEFAULT_TIMEOUT_SECONDS と同一の named
        constant を import して参照している(独立した重複定義ではない)。"""
        import baseline_vc_preflight  # noqa: E402 (sys.path はモジュールロード時に設定済み)

        assert (
            _rcr_mod._VC_PREFLIGHT_PER_COMMAND_TIMEOUT
            == baseline_vc_preflight.DEFAULT_TIMEOUT_SECONDS
        )

    def test_vc_preflight_timeout_is_derived_relationship_not_bare_literal(self):
        """_VC_PREFLIGHT_TIMEOUT = per-command timeout × budget + overhead の
        関係式で導出されており、これらの named constant から再計算した値と
        一致する(単純な独立リテラル引き上げではないことを固定する)。"""
        assert hasattr(_rcr_mod, "_VC_PREFLIGHT_MAX_COMMAND_BUDGET")
        assert hasattr(_rcr_mod, "_VC_PREFLIGHT_OVERHEAD_SECONDS")

        expected = (
            _rcr_mod._VC_PREFLIGHT_PER_COMMAND_TIMEOUT
            * _rcr_mod._VC_PREFLIGHT_MAX_COMMAND_BUDGET
            + _rcr_mod._VC_PREFLIGHT_OVERHEAD_SECONDS
        )
        assert _rcr_mod._VC_PREFLIGHT_TIMEOUT == expected

    def test_vc_preflight_timeout_safely_exceeds_measured_test_runtime(self):
        """実測 test_baseline_vc_preflight.py 実行時間(約58秒)を安全に上回る。"""
        assert _rcr_mod._VC_PREFLIGHT_TIMEOUT >= 180

    def test_source_does_not_hardcode_independent_literal(self):
        """回帰防止: `_VC_PREFLIGHT_TIMEOUT = <bare int literal>` の
        独立リテラル代入に戻っていないことをソース検査で固定する。"""
        with open(_RCR_PATH) as f:
            script_content = f.read()

        assert "_VC_PREFLIGHT_TIMEOUT = 180" not in script_content
        assert "_VC_PREFLIGHT_TIMEOUT = 600" not in script_content
        assert "_VC_PREFLIGHT_TIMEOUT = 900" not in script_content
        assert (
            "_VC_PREFLIGHT_TIMEOUT = (\n"
            "    _VC_PREFLIGHT_PER_COMMAND_TIMEOUT * _VC_PREFLIGHT_MAX_COMMAND_BUDGET\n"
            "    + _VC_PREFLIGHT_OVERHEAD_SECONDS\n"
            ")"
        ) in script_content


# ---------------------------------------------------------------------------
# AC3: --timeout-seconds is passed explicitly as argv to baseline_vc_preflight.py
# ---------------------------------------------------------------------------


class TestBaselineVcPreflightTimeoutArgvAC3:
    """AC3: baseline_vc_preflight.py の subprocess 起動 argv に
    --timeout-seconds が明示的に含まれることを固定する。"""

    def test_timeout_seconds_argv_passed_to_baseline_vc_preflight(self):
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_json = _make_vc_preflight_json("pass")

        run_script_iter = iter(
            [
                (readiness_json, 0, None),
                (product_spec_json, 0, None),
                (vc_json, 0, None),
            ]
        )
        captured_calls = []

        def _fake_run_script(cmd, *args, **kwargs):
            captured_calls.append(cmd)
            return next(run_script_iter)

        with patch.object(_rcr_mod, "_run_script", side_effect=_fake_run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"

        # vc_preflight is the 3rd _run_script call (readiness, product_spec, vc_preflight)
        vc_preflight_cmd = captured_calls[2]
        assert str(_rcr_mod._BASELINE_VC_PREFLIGHT_PY) in vc_preflight_cmd
        assert "--timeout-seconds" in vc_preflight_cmd

        timeout_idx = vc_preflight_cmd.index("--timeout-seconds")
        timeout_value = vc_preflight_cmd[timeout_idx + 1]
        assert timeout_value == str(_rcr_mod._VC_PREFLIGHT_PER_COMMAND_TIMEOUT)



# ---------------------------------------------------------------------------
# Issue #1338 AC9: run_contract_review_once.py holds a named constant
# _VC_PREFLIGHT_MAX_WORKERS and passes it, together with --timeout-seconds,
# as explicit argv when invoking baseline_vc_preflight.py.
# ---------------------------------------------------------------------------


class TestVcPreflightMaxWorkersWiringAC9:
    """AC9: _VC_PREFLIGHT_MAX_WORKERS named constant + explicit argv wiring."""

    def test_named_constant_exists_with_expected_initial_value(self):
        assert hasattr(_rcr_mod, "_VC_PREFLIGHT_MAX_WORKERS")
        assert _rcr_mod._VC_PREFLIGHT_MAX_WORKERS == 2

    def test_vc_preflight_invocation_passes_explicit_max_workers_and_timeout(self):
        """The baseline_vc_preflight.py subprocess argv explicitly includes both
        --timeout-seconds and --max-workers (sourced from the named constants),
        not merely relying on the sub-script's own defaults."""
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_json = _make_vc_preflight_json("pass")

        run_script_iter = iter(
            [
                (readiness_json, 0, None),
                (product_spec_json, 0, None),
                (vc_json, 0, None),
            ]
        )
        captured_calls = []

        def _fake_run_script(cmd, *args, **kwargs):
            captured_calls.append(cmd)
            return next(run_script_iter)

        with patch.object(_rcr_mod, "_run_script", side_effect=_fake_run_script):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"

        # vc_preflight is the 3rd _run_script call (readiness, product_spec, vc_preflight)
        vc_preflight_cmd = captured_calls[2]
        assert str(_rcr_mod._BASELINE_VC_PREFLIGHT_PY) in vc_preflight_cmd

        assert "--timeout-seconds" in vc_preflight_cmd
        timeout_idx = vc_preflight_cmd.index("--timeout-seconds")
        assert vc_preflight_cmd[timeout_idx + 1] == str(_rcr_mod._VC_PREFLIGHT_PER_COMMAND_TIMEOUT)

        assert "--max-workers" in vc_preflight_cmd
        max_workers_idx = vc_preflight_cmd.index("--max-workers")
        assert vc_preflight_cmd[max_workers_idx + 1] == str(_rcr_mod._VC_PREFLIGHT_MAX_WORKERS)
        assert vc_preflight_cmd[max_workers_idx + 1] == "2"
