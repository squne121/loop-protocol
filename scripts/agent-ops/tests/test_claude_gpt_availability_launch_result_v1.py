"""Issue #2950: claude-gpt live causal-evidence test の可用性判定 helper が、
current ``CLAUDE_GPT_LAUNCH_RESULT_V1`` receipt に exact に bind していることを検証する。

- AC1-AC4: receipt 解釈の pure 関数に対する fixture / poison fixture test。
- AC5/AC6: 実 ``scripts/claude-gpt/preflight.sh`` を local loopback の ``FakeServer`` に対して
  実行する runtime test。preflight 自身の exit code と receipt を helper の返り値とは独立に
  assert してから helper を評価する（起動失敗 OSError / timeout を False に変換しただけの結果は
  PASS 根拠にしない）。``sh`` または loopback が使えない環境では SKIP ではなく fail させる
  （SKIP は PASS ではない）。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_CHECKOUT_ROOT = _TESTS_DIR.parent.parent.parent
_HELPER_PATH = _TESTS_DIR / "test_run_worktree_agent_runtime_smoke_claude_gpt_live_causal_evidence.py"
_HARNESS_PATH = _CHECKOUT_ROOT / "scripts" / "claude-gpt" / "tests" / "_launcher_harness.py"
_SCHEMA = "CLAUDE_GPT_LAUNCH_RESULT_V1"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


helper = _load("claude_gpt_live_causal_evidence_helper_issue_2950_availability", _HELPER_PATH)
harness = _load("claude_gpt_launcher_harness_issue_2950_availability", _HARNESS_PATH)


def _ok_receipt(**overrides) -> dict:
    receipt = {
        "schema": _SCHEMA,
        "status": "ok",
        "mode": "check_only",
        "connected_server": {"reachable": True, "model_catalog_ok": True},
        "local_proxy_binary_auxiliary": {"path": "/nonexistent/claude-code-proxy"},
        "launch_env": {},
    }
    receipt.update(overrides)
    return receipt


def _interpret(exit_code: int, payload) -> tuple[bool, str]:
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return helper._interpret_claude_gpt_launch_result(exit_code, stdout)


def test_availability_receipt_ok_returns_true() -> None:
    available, detail = _interpret(0, _ok_receipt())
    assert available is True, detail


def test_availability_failed_exit7_returns_false_with_reason() -> None:
    receipt = {
        "schema": _SCHEMA,
        "status": "failed",
        "reason": "connected_server_unreachable",
        "cause": "no_server_listening_or_not_responding",
        "connected_server": {"reachable": False, "model_catalog_ok": False},
    }
    available, reason = _interpret(7, receipt)
    assert available is False
    assert "failed" in reason
    assert "7" in reason


def test_availability_legacy_shape_returns_false() -> None:
    legacy = {
        "exit_code": 0,
        "binary_available": True,
        "proxy": {"absolute_path": "/usr/local/bin/claude-code-proxy"},
        "chatgpt_auth": {"available": True},
    }
    available, reason = _interpret(0, legacy)
    assert available is False
    assert reason


@pytest.mark.parametrize(
    ("exit_code", "payload"),
    [
        pytest.param(0, _ok_receipt(schema="CLAUDE_GPT_LAUNCH_RESULT_V0"), id="wrong-schema"),
        pytest.param(0, {k: v for k, v in _ok_receipt().items() if k != "schema"}, id="missing-schema"),
        pytest.param(0, [], id="top-level-empty-list"),
        pytest.param(0, [_ok_receipt()], id="top-level-list-of-receipt"),
        pytest.param(0, "null", id="top-level-null"),
        pytest.param(0, "0", id="top-level-scalar"),
        pytest.param(0, '"ok"', id="top-level-string"),
        pytest.param(0, _ok_receipt(connected_server="reachable"), id="connected-server-string"),
        pytest.param(0, _ok_receipt(connected_server=1), id="connected-server-scalar"),
        pytest.param(0, _ok_receipt(connected_server=None), id="connected-server-null"),
        pytest.param(0, "not json at all", id="non-json"),
        pytest.param(0, "", id="empty-stdout"),
        pytest.param(
            0,
            _ok_receipt(connected_server={"reachable": False, "model_catalog_ok": True}),
            id="reachable-false",
        ),
        pytest.param(
            0,
            _ok_receipt(connected_server={"reachable": True, "model_catalog_ok": False}),
            id="model-catalog-ok-false",
        ),
        pytest.param(
            0,
            _ok_receipt(connected_server={"reachable": "true", "model_catalog_ok": True}),
            id="reachable-truthy-non-bool",
        ),
        pytest.param(0, _ok_receipt(mode="launch"), id="wrong-mode"),
        pytest.param(7, _ok_receipt(), id="status-ok-with-nonzero-exit"),
        pytest.param(0, _ok_receipt(status="failed"), id="status-failed-with-zero-exit"),
    ],
)
def test_availability_invalid_receipt_shape_returns_false(exit_code: int, payload) -> None:
    available, reason = _interpret(exit_code, payload)
    assert available is False
    assert isinstance(reason, str) and reason


def _require_sh_and_loopback() -> None:
    """Fail (not skip) when the runtime AC environment is missing: SKIP is not PASS."""
    assert shutil.which("sh"), "AC5/AC6 require `sh` (SKIP is not PASS)"
    assert helper._PREFLIGHT_PATH.is_file(), f"missing {helper._PREFLIGHT_PATH}"


def _run_real_preflight(env: dict) -> tuple[int, dict]:
    """Run the real preflight.sh and independently decode its own exit code and receipt."""
    exit_code, stdout, error = helper._run_claude_gpt_preflight(env)
    assert error is None, f"preflight.sh could not be run (not a valid AC5/AC6 basis): {error}"
    assert exit_code is not None
    receipt = json.loads(stdout)
    assert isinstance(receipt, dict)
    assert receipt.get("schema") == _SCHEMA
    return exit_code, receipt


def test_availability_real_preflight_loopback_success_returns_true(tmp_path: Path) -> None:
    _require_sh_and_loopback()
    with harness.FakeServer(models=harness.REQUIRED_MODELS) as server:
        assert server.alive() and server.listening()
        env = harness.base_env(tmp_path, ANTHROPIC_BASE_URL=server.url)
        # Independent of the helper: preflight's own exit code and receipt.
        exit_code, receipt = _run_real_preflight(env)
        assert exit_code == 0, f"preflight exit_code={exit_code} receipt={receipt}"
        assert receipt["status"] == "ok"
        assert receipt["mode"] == "check_only"
        assert receipt["connected_server"]["reachable"] is True
        assert receipt["connected_server"]["model_catalog_ok"] is True

        available, detail = helper._claude_gpt_available(env)
    assert available is True, detail


def test_availability_real_preflight_failure_returns_false(tmp_path: Path) -> None:
    _require_sh_and_loopback()

    def _assert_failed_preflight(env: dict) -> None:
        exit_code, receipt = _run_real_preflight(env)
        assert exit_code == 7, f"preflight exit_code={exit_code} receipt={receipt}"
        assert receipt["status"] == "failed"
        available, reason = helper._claude_gpt_available(env)
        assert available is False
        assert "failed" in reason, reason
        assert "exit_code=7" in reason, reason

    # Closed port: nothing is listening.
    closed_env = harness.base_env(tmp_path, ANTHROPIC_BASE_URL=f"http://127.0.0.1:{harness.closed_port()}")
    _assert_failed_preflight(closed_env)

    # Incomplete model catalog: reachable server that lacks a required model.
    with harness.FakeServer(models=(harness.REQUIRED_MODELS[0],)) as server:
        assert server.alive() and server.listening()
        incomplete_env = harness.base_env(tmp_path, ANTHROPIC_BASE_URL=server.url)
        _assert_failed_preflight(incomplete_env)
